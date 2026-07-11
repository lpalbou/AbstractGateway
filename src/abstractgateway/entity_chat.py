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

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

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
# memories to function"). Arithmetic honest at 65536: token budget 12% = 7864;
# 36 seats x ~200-token rich digests = 7200 <= 7864 — seats still fill.
DEFAULT_ENTITY_CHAT_SHELF_SIZE = 36
DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW = 65536
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
    "the operator decides; the gateway never falls back on its own"
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

        return read_home_substrate(Path(home_dir))
    except ImportError:
        pass  # older runtime without identity.substrate — parse locally

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
        return {"provider": p, "model": m} if (p and m) else {}
    except Exception:
        return {}


def write_entity_substrate(home_dir: Any, *, provider: str, model: str) -> None:
    """Persist the operator's one-per-entity substrate choice (his home,
    operator-owned like tool_policy.yaml; the entity's tools cannot touch
    the home root)."""
    from pathlib import Path

    p, m = str(provider or "").strip(), str(model or "").strip()
    if not p or not m:
        raise ValueError("substrate needs BOTH provider and model (explicit operator choice)")
    import yaml

    (Path(home_dir) / SUBSTRATE_FILENAME).write_text(
        yaml.safe_dump({"provider": p, "model": m}, sort_keys=True), encoding="utf-8"
    )


def resolve_substrate(
    provider: Optional[str], model: Optional[str], *, home_dir: Any = None
) -> Tuple[str, str]:
    """Request override > home substrate.yaml > operator env > refuse.
    Never a code default (NO FALLBACK)."""
    import os as _os

    p = (provider or "").strip()
    m = (model or "").strip()
    if not (p and m) and home_dir is not None:
        stored = read_entity_substrate(home_dir)
        p = p or stored.get("provider", "")
        m = m or stored.get("model", "")
    p = p or (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER") or "").strip()
    m = m or (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL") or "").strip()
    if not p or not m:
        raise ChatOpenRefused(400, SUBSTRATE_REFUSAL)
    return p, m


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

    def __init__(self, provider: str, *, model: str, **llm_kwargs: Any) -> None:
        from abstractruntime.integrations.abstractcore import LocalAbstractCoreLLMClient

        self._client = LocalAbstractCoreLLMClient(provider=provider, model=model, llm_kwargs=dict(llm_kwargs))

    def generate(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        kwargs.setdefault("prompt", "")
        out = self._client.generate(**kwargs)
        if isinstance(out, dict):
            return SimpleNamespace(**{"content": out.get("content"), **{k: v for k, v in out.items() if k != "content"}})
        return out


def _default_llm_factory(provider: str, **kwargs: Any) -> Any:
    model = str(kwargs.pop("model", "") or "")
    return _RuntimeLLMAdapter(provider, model=model, **kwargs)


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
    last_activity: float = 0.0
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
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
    ) -> None:
        self._registry = registry
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
        # Output headroom (agency-caps audit, maintainer 2026-07-11): parity
        # with the visit LLM handler — a report/rich-answer turn must not be
        # cut mid-thought. Per-entity operator config is queued for the
        # creation modal (substrate config is already surfaced there).
        max_output_tokens: int = 4096,
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

        # ONE substrate per entity (maintainer ruling 2026-07-09 06:32:
        # visits and own time share the SAME mind). Request override > the
        # home's persisted substrate.yaml > operator env > loud refusal.
        provider, model = resolve_substrate(provider, model, home_dir=home_dir)

        with self._lock:
            self._reap_idle_locked()
            live = self._by_slug.get(slug)
            if live is not None:
                raise ChatOpenRefused(
                    409,
                    f"a chat session is already open on {manifest.entity_id} (chat_id {live!r}) — "
                    "one life, one summon; close it or let it idle out",
                )

        state = read_entity_state(home_dir)
        mode = str(state.get("mode") or "")
        reason = str(state.get("reason") or "")
        if state.get("state") == "paused":
            raise ChatOpenRefused(409, f"{manifest.entity_id} is paused (hard freeze): {reason or 'no reason recorded'}")

        # Auto-yield (--pause-loop parity, HTTP-bounded).
        #
        # The loop is a live process whenever phase is "day" OR "between" (it
        # re-opens a day on state=awake at its next top-gate). Yielding ONLY
        # on phase=="day" left a reachable race (observer, maintainer
        # 2026-07-09 02:02): a visit opened while the loop rested "between"
        # got yielded_loop=false, then the loop re-entered a day mid-visit —
        # a genuine visit+own-time overlap, not a display artifact. Fix: yield
        # whenever the loop is ALIVE (write the visiting state FIRST so the
        # loop's state=awake top-gate cannot re-open a day, then wait for
        # quiescence).
        yielded = False
        visitor = (participants or ["person:operator"])[0]
        loop_alive = bool(read_loop_status(home_dir).get("running"))
        if loop_alive:
            write_entity_state(
                home_dir, "asleep",
                reason=f"in conversation with {visitor} (auto-yield)",
                mode="visiting",
            )
            if not await_loop_quiescent(home_dir, timeout_seconds=self._yield_wait_s):
                # Restore awake so a failed open never strands the loop asleep.
                write_entity_state(home_dir, "awake", reason="visit open aborted (loop did not yield in time)")
                raise ChatOpenRefused(
                    409,
                    "the entity's own-time loop has not reached a tick boundary yet — "
                    f"waited {self._yield_wait_s:.0f}s; retry shortly (the yield request stands)",
                )
            yielded = True
        elif state.get("state") == "asleep":
            if mode == "visiting" or "auto-yield" in reason:
                # A stale/standing visit posture is adopted WITH the duty to wake.
                yielded = True
            else:
                raise ChatOpenRefused(
                    409,
                    f"{manifest.entity_id} is asleep ({reason or 'no reason recorded'}) — "
                    "wake him first (`entity wake`) or let his loop negotiate the visit",
                )

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
                if yielded:
                    write_entity_state(home_dir, "awake", reason="visit open aborted (home has a writer)")
                raise ChatOpenRefused(409, str(e))
        except ImportError:
            # Older runtime without storage.lease: no site acquires (the
            # loop's sites are absent too) — labeled, never silent.
            env_warnings.append(
                "#FALLBACK runtime predates the home lease; visit opened without the writer mutex"
            )

        try:
            home = open_home(home_dir, embedder=self._registry._resolve_embedder())
            llm_kwargs: Dict[str, Any] = {"model": model, "max_output_tokens": int(max_output_tokens)}
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
                model_info={"provider": str(provider), "model": str(model)},
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
            if yielded:
                write_entity_state(home_dir, "awake", reason="visit aborted (prelude refused)")
            raise ChatOpenRefused(409, f"summon refused: {'; '.join(quiet) or e}")
        except BaseException:
            if lease is not None:
                lease.release()
            if yielded:
                write_entity_state(home_dir, "awake", reason="visit aborted (open failed)")
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

        chat_id = f"chat-{uuid.uuid4().hex[:12]}"
        hosted = _HostedChat(
            chat_id=chat_id,
            entity_slug=slug,
            entity_id=manifest.entity_id,
            session=session,
            home=home,
            yielded_loop=yielded,
            opened_at=datetime.now(timezone.utc).isoformat(),
            model_info={"provider": str(provider), "model": str(model)},
            lease=lease,
            last_activity=_time.monotonic(),
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
            reply, report = hosted.session.turn(
                user_text, **({"speaker_label": speaker_label} if speaker_label else {})
            )
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
            "model_info": dict(hosted.model_info),
        }

    # The ONE composite phase (observer's ask c, maintainer 2026-07-09 02:02:
    # the header showed VISITING + RESTING + own-time-lit at once because the
    # client re-derived state from three raw sources and they contradicted).
    # Computed SERVER-SIDE with a single precedence so clients never re-derive
    # (and re-bug) it. Mutually exclusive by construction: exactly one `phase`.
    _LIFE_PHASES = ("visiting", "paused", "asleep", "own_time", "resting", "awake")

    def life_state(self, name: str) -> Dict[str, Any]:
        """The gateway-computed life phase — one mutually-exclusive answer the
        observer consumes directly. Precedence (visiting wins, matching the
        client contract it replaces): a visit outranks everything (the loop is
        auto-yielded under it); operator paused/asleep outrank own-time; a
        running loop is own_time (mid-day, phase=day) or resting (between
        days); awake is the floor. `own_time_running` is reported alongside so
        a viewer can show the loop is alive WITHOUT contradicting the phase."""
        from .entity_loop import loop_status

        manifest = self._registry.manifest_for(name)
        home_dir = self._registry.entities_dir / manifest.slug

        chat = self.status(name)
        state = self._registry.state_of(name)
        loop = loop_status(home_dir)
        loop_running = bool(loop.get("running"))
        loop_phase = str(loop.get("phase") or "")

        if chat.get("open"):
            phase = "visiting"
        elif str(state.get("state") or "awake") == "paused":
            phase = "paused"
        elif str(state.get("state") or "awake") == "asleep":
            phase = "asleep"
        elif loop_running and loop_phase == "day":
            phase = "own_time"
        elif loop_running:
            phase = "resting"
        else:
            phase = "awake"

        return {
            "entity_id": manifest.entity_id,
            "phase": phase,
            "chat_open": bool(chat.get("open")),
            "chat_id": chat.get("chat_id"),
            "state": state.get("state"),
            "state_mode": state.get("mode"),
            "state_reason": state.get("reason"),
            "own_time_running": loop_running,
            "own_time_phase": loop_phase or None,
        }

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

    def close_all(self) -> None:
        """Shutdown hygiene: reflect + close every live session (the loop
        wake duty travels with each)."""
        with self._lock:
            hosted_all = [h for h in self._sessions.values() if not h.closed]
            for h in hosted_all:
                h.closed = True
        for h in hosted_all:
            try:
                self._close_hosted(h, reflect=True)
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
            if hosted.yielded_loop:
                try:
                    # Wake-cue seeding, gateway half (maintainer escalation
                    # 2026-07-09, R3 — "agency blindness"): the auto-yield
                    # return reason is what the loop's honest-waking rule
                    # reads back to him, so it should carry the VISIT's
                    # facts (who came, how long, what he elected to pursue)
                    # instead of a generic line — his next day can begin
                    # from the visit instead of the bridges default. The
                    # cue construction from this reason is runtime's half.
                    visitors = [p for p in hosted.session.participants if not p.startswith("entity:")]
                    reason = (
                        f"visitor session ended ({', '.join(visitors) or 'a visitor'}; "
                        f"{len(hosted.session.reports)} turns)"
                    )
                    interests = [words for _rid, words in (reflection or {}).get("interests", []) if words]
                    if interests:
                        reason += f" — you elected to pursue: {'; '.join(str(w)[:120] for w in interests[:2])}"
                    write_entity_state(
                        self._registry.entities_dir / hosted.entity_slug,
                        "awake",
                        reason=reason,
                    )
                except Exception as e:
                    warnings.append(f"#FALLBACK could not wake the loop: {e} — wake him manually")
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
        stale = [
            h for h in self._sessions.values()
            if not h.closed and (now - h.last_activity) > self._idle_timeout_s
        ]
        for h in stale:
            h.closed = True
        if not stale:
            return
        # Close outside caller-visible state but inside our lock scope is
        # deadlock-prone (reflect calls the LLM); release-and-close instead.
        def _close_later() -> None:
            for h in stale:
                try:
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
