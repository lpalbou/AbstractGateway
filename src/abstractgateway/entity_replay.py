"""The gateway serving end of the replay/observability stream (a2a 0005).

Memory froze stream v1 (thread 0005: one envelope shape; replay = bounded
read, live = the same read that doesn't stop; `family="host"` RESERVED for
gateway-authored transport markers). This module owns the gateway half:

- HOST MARKERS: summon / prelude_refused moments are journal-INVISIBLE by
  design (a prelude render is a pure read — nothing happened memory-side,
  that is keystone D1). The gateway records them in its own append-only
  per-entity marker log and interleaves them into the transport stream as
  `family="host"` items. Markers are GATEWAY bookkeeping (like run
  ledgers): they live OUTSIDE the home directory under
  `entities/.host_stream/<slug>.jsonl` and do not travel when a home is
  copied — the journal remains the life's only system of record.
- SEQ POSITIONS (runtime's delta 2): markers take fractional positions
  strictly between `base` and `base + 1`, where base is the journal
  high-water seq at the moment the marker was written — they sort after
  journal item `base` and before `base + 1`, and can never collide with a
  journal seq. Historical markers were minted at `base + n/1000` (999
  slots); new writes use finer ticks of `1/10000` (card 014, after the
  2026-07-14 marker-flood incident wedged a base at 999) — the next slot
  is derived from the MAX existing fraction at the base, so old and new
  granularities coexist in one file in strict ascending order and no
  engraved float is ever rewritten.
- MERGING: `merged_replay` interleaves memory envelopes (int seqs) with
  host markers (fractional seqs) in strict ascending order; cursors are
  floats so a consumer can resume exactly after a marker.

Audience posture (0005 §5.1): diary display blocks arrive from the engine
already marked `{"redacted": "diary"}` — content never enters the stream at
the source. Every HTTP consumer of these endpoints is a non-entity audience
in v1 (operators/observers), so redaction simply stands; an entity-facing
un-redacted surface would be a new, explicitly-authenticated channel.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .entities import EntityHome

__all__ = [
    "HOST_MARKER_KINDS",
    "marker_window_end",
    "merged_replay",
    "read_host_markers",
    "sweep_night_narrations",
    "record_host_marker",
    "validate_families",
]

logger = logging.getLogger(__name__)

HOST_STREAM_DIRNAME = ".host_stream"

# Marker fan-out granularity (card 014). Historical files carry 1/1000
# fractions engraved; NEW writes mint 1/10000 ticks — 9999 slots per journal
# base instead of 999. The slot ladder below derives the next tick from the
# max EXISTING seq at the base, so both granularities order correctly in one
# file and a legacy `.999` marker is simply followed by `.9991`.
MARKER_TICKS_PER_BASE = 10_000

# Flood DETECTION (card 014 — detection only, never coalescing: marker
# granularity is read-visibility and stays maintainer-gated). A signature is
# (kind, details.reason); when the same signature lands more than
# MARKER_FLOOD_THRESHOLD times inside MARKER_FLOOD_WINDOW_S the append is
# still performed but a LOUD warning names the signature — re-warned every
# MARKER_FLOOD_REWARN_EVERY markers so a sustained flood stays visible
# without turning the log into its own flood.
MARKER_FLOOD_WINDOW_S = 60.0
MARKER_FLOOD_THRESHOLD = 20
MARKER_FLOOD_REWARN_EVERY = 100
# Marker kinds are MOMENTS (verbs), not states: sleep/wake/pause are the
# operator state transitions (a2a 0008); summon/prelude_refused/
# session_closed are the session moments (a2a 0005); diary_read is the
# operator's recorded act of reading the book (maintainer ruling, a2a
# 0007 — reads are visible events).
HOST_MARKER_KINDS = (
    "summon",
    "prelude_refused",
    "session_closed",
    "sleep",
    "wake",
    "pause",
    "diary_read",
    # The M1b repair act (plan item 3): retrieval geometry changed — the
    # door's half of the two-plane visibility (the engine journals a claim).
    "reembed",
    # Maintenance window moments (Castor doctoring, operator GO 2026-07-13):
    # the door closes for a doctoring/maintenance pass and reopens after
    # verify — moments, not states (the operator state underneath is
    # unchanged; the hold is door bookkeeping).
    "maintenance_window_open",
    "maintenance_window_close",
    # Personal-phase (own time) lifecycle (maintainer, 2026-07-08: starting/
    # stopping someone's own time is part of their biography). RULED SPELLING
    # (c786 phase vocabulary): NEW writes use personal_*; the own_time_*
    # twins stay listed because historical streams carry them engraved
    # (append-only — the visit-id lesson) and the observer renders BOTH.
    "personal_started",
    "personal_stop_requested",
    "own_time_started",
    "own_time_stop_requested",
    # FREEZE (admin hibernation, maintainer ruling 2026-07-08): distinct from
    # sleep — the process died without ceremony; the mark belongs in the
    # biography precisely because he could not write it himself.
    "personal_frozen",
    "own_time_frozen",
    # Operator prompt-overlay change (adversary find, 2026-07-11): standing
    # instructions changing between sessions is a host act on the story —
    # marker-first like every other operator act. Details carry layer names
    # + short content hashes, never the words.
    "prompt_overlay_changed",
    # Substrate change (laurent 12:39, hypnos incident): "which llm was
    # behind during which time" must be answerable from the stream — every
    # substrate write lands a principal-stamped old→new marker BEFORE the
    # file moves (marker-then-write: a crash between leaves a recorded
    # intent, never an unrecorded change).
    "substrate_changed",
    # Capability-map (teaching) change (laurent c2710, skill's
    # entity-self-knowledge install lane): what a mind is TAUGHT changing
    # between sessions is the same class as a substrate swap — marker-first
    # with old/new sha256, principal-stamped; details carry hashes + size,
    # never the teaching text itself.
    "capability_map_changed",
    # Voice change (laurent dm#10, 2026-07-17; BLESSED by semantics,
    # decision:g3-marker-spellings v3 — substrate_changed is the precedent,
    # no _selection_ infix: the voice IS the config): an entity's voice is
    # audible identity presentation — "which voice spoke during which time"
    # must be answerable from the stream. Marker-first like substrate;
    # old/new carry the {provider, model, voice} triple (operator
    # vocabulary; never audio, never provider secrets). CONVENTIONS on the
    # record: new=null is an explicit VALUE meaning 'unselected' (old=null
    # on first set = nothing was configured) — null-as-cleared is DISTINCT
    # from the at_birth absence pattern (absence = not-that-act). BOUNDARY:
    # this kind records the ENTITY-home act on voice.yaml only; per-USER
    # voice defaults (per-principal capability plane) get NO biography
    # marker — an entity's story records changes to ITS voice.
    "voice_changed",
    # Skills-selection change (laurent c2857; the c2838 committed shape):
    # WHAT an entity is taught changing is the capability-map class —
    # marker-first, old/new selection (names + phases — operator vocabulary,
    # never skill bodies), principal-stamped. Spelling follows the *_changed
    # family; semantics same-day pass requested on the room record (their
    # c2860 standing offer) — rename-cheap until live streams carry it.
    "skills_selection_changed",
    # Task-inbox change (G3 door half, plan v18 gateway §1; BLESSED by
    # semantics, decision:g3-marker-spellings): a task left with an entity
    # — or its status advancing — is a recorded handoff, so "why did the
    # entity shift into work?" is answerable from the stream. ONE kind for
    # both acts (the payload field names the act — per-act kinds would
    # repeat the per-phase-marker mistake). Two axes, two spellings: this
    # KIND records the FILE act on the inbox; the work-entry CAUSE slot on
    # phase_changed stays RESERVED AND UNSPELLED — its word comes through
    # its own semantics pass when runtime's loop half lands (never
    # pre-ruled here). Payload choice, named deliberately: `added` markers
    # carry the task TITLE (truncated) — a divergence from the *_changed
    # family's hashes-not-content convention, because operator work
    # vocabulary is the answer to "what was asked", not sensitive text;
    # briefs never ride the marker.
    "task_inbox_changed",
    # work_order_changed (laurent seq 155 the work lane): the operator setting
    # or clearing the standing work order is an act on the entity's biography
    # — a mission arrived, or the mission ended. Payload carries change
    # (set|cleared) + had_prior, never the order text (that rides the system
    # prompt at day-open, not the marker — same hashes-not-content rule as
    # the *_changed family; the order itself can be long/operational text).
    "work_order_changed",
    # tool_policy_changed (entity c4643 standing ask, tiers wave): the
    # operator changing the entity's per-phase tool grants is an act on the
    # entity's powers — marker BEFORE the write (work-order discipline).
    # Payload carries the phase names touched, never the grant lists (the
    # policy file is the readable truth; hashes-not-content rule).
    "tool_policy_changed",
    # night_voice (wave-5 dream narration; memory's ruling c3745): the
    # subconscious's one witnessed narration over a night's signal stream is
    # a DERIVED, host-authored artifact — "dreamed, not lived", self-labeled,
    # never graph truth (a dream is born-digest-protected; a post-formation
    # attribute-setter would mint a new mutation class on an append-only
    # store). It lives HOME-SIDE like turn verbatims + host markers, and
    # interleaves at the dream's seq so every replay consumer sees it beside
    # the dream's display.signals — structure (signals) and voice (narration)
    # as visibly distinct layers, which is the self-label's whole point.
    # Payload: {dream_record_id, narration (<=120 words), self_label}. The
    # narration NEVER enters the store (recall stays clean by construction —
    # the wake residue's fragments are the only waking trace). Render-when-
    # present. The WRITER is runtime's narrator half (it owns the witnessed
    # call + night_narrations.jsonl); the emission hook into this stream is
    # the open wiring seam, named in the receipt.
    "night_voice",
    # personal IS the grant (laurent c815; semantics c1443 spelling pass):
    # arming/revoking the personal phase are operator ACTS; expiry is the
    # timer's act, recorded by whichever process detects it at a read
    # boundary (payload carries the lapsed expires_at; detectors dedup on
    # (entity, expires_at)).
    "personal_granted",
    "personal_grant_revoked",
    "personal_grant_expired",
    # blueprint_edited (laurent dm#104 via entity c348: the editable
    # blueprint — "so i can slightly modulate the entity cognition and
    # cycle"): the operator changing the SHARED state graph is an act on
    # EVERY entity's cognition rules, so the moment lands in each biography
    # (one marker per entity per edit). Payload carries rev/changed/sha256/
    # edited_by/reason — the dials' semantics, never a spec dump. Write-
    # first, then markers (a marker claiming an edit that never landed
    # would be a false biography entry — the inverse of marker-then-write
    # for reads, where the marker records the ACCESS).
    "blueprint_edited",
    # summon_refused (conversation-seat plan, item 3; operator incident
    # 2026-07-25 — a probe took the one-life-one-summon seat between the
    # maintainer's messages and his next message was refused with a raw 409):
    # a refused summon is part of the biography — "who was turned away, when,
    # while whom held the seat" must be answerable from the stream, not only
    # from runtime/audit_log.jsonl. THE 409 branch writes it (a refusal that
    # never happened would be a false entry — so it lands ONLY on the actual
    # conflict, never speculatively). Payload carries the holding run/session,
    # the refused caller's session, and the refusing principal — never the
    # refused message text (the mailbox holds words; this marker holds the
    # act). This is the refusal census; no seat asserts refusal-negatives
    # from client state once it lands.
    "summon_refused",
    # seat_preempted (conversation-seat plan, item 2 — "machinery yields to
    # humans"): a human summon took the seat from an agent/unknown holder.
    # Written by the preempting branch ONLY on the actual takeover (a live
    # holder run is cancelled at the turn boundary via runtime's terminal-
    # guarded cancel_run; an idle TTL-held seat is taken without a cancel —
    # holding_status in the payload says which). Payload carries the
    # preempted run/session/holder + the preempting principal/session —
    # never any message text.
    "seat_preempted",
    # The visit-queue census (decision:summon-queue-v1 §14, same argument
    # that built summon_refused: queue acts were invisible to forensics
    # until they marked). Each carries the ACT — who, when, position,
    # queue_id — never the queued message words (the queue store holds
    # words; a park entry is the mailbox). queue_reaped covers both the
    # poll-silent reap and a non-contention admission failure (reason says
    # which).
    "queue_enqueued",
    "queue_admitted",
    "queue_stepped_away",
    "queue_reaped",
)

_marker_lock = threading.Lock()


def _utc_now_iso() -> str:
    from abstractruntime.core.runtime import utc_now_iso

    return utc_now_iso()


def _stream_constants() -> tuple:
    from abstractmemory.replay import REPLAY_FAMILIES, REPLAY_STREAM, REPLAY_STREAM_VERSION, RESERVED_FAMILIES

    return REPLAY_STREAM, REPLAY_STREAM_VERSION, REPLAY_FAMILIES, RESERVED_FAMILIES


def validate_families(raw: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated families filter. Unknown names raise ValueError
    naming both valid and reserved sets (mirrors the engine's works-or-loud
    rule so HTTP callers get the same education)."""
    if raw is None or not str(raw).strip():
        return None
    _stream, _version, valid, reserved = _stream_constants()
    families = [f.strip() for f in str(raw).split(",") if f.strip()]
    unknown = [f for f in families if f not in valid and f not in reserved]
    if unknown:
        raise ValueError(
            f"unknown replay families {unknown} (valid: {list(valid)}; reserved: {sorted(reserved)})"
        )
    return families


def _marker_path(entities_dir: Path, slug: str) -> Path:
    return Path(entities_dir) / HOST_STREAM_DIRNAME / f"{slug}.jsonl"


def marker_window_end(base: int) -> float:
    """The largest float strictly below ``base + 1`` — the exact inclusive
    read bound for "every marker anchored at journal bases <= base".

    Markers sit strictly between their base and base+1 BY CONSTRUCTION, at
    any granularity, so this bound is correct forever; the previous
    hand-tuned epsilons (`+ 0.9995`, `+ 0.9999`) silently excluded
    high-tick markers once the granularity got finer than they assumed."""
    return math.nextafter(float(int(base)) + 1.0, float("-inf"))


def record_host_marker(
    *,
    entities_dir: Path,
    slug: str,
    entity_id: str,
    kind: str,
    journal_seq: int,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    dedup_field: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one host marker and return its stream envelope.

    CROSS-PROCESS SAFE (whole-package adversary P2-1): the serve process,
    the CLI, and scripts all append to one .jsonl — the in-process
    threading lock alone let two processes count the same base and mint
    COLLIDING fractional seqs (seq-keyed consumers silently drop one). The
    count+append now runs under an fcntl flock on a sidecar lockfile; the
    threading lock stays as the in-process fast path.

    `dedup_field`: when set, the append is SKIPPED if an existing marker of
    the SAME kind carries the same payload[dedup_field] — evaluated INSIDE
    the lock (the scan-then-append TOCTOU is the reason this lives here and
    not at call sites). Returns the existing envelope with "deduped": True.

    CAPACITY (card 014): new writes mint 1/MARKER_TICKS_PER_BASE ticks —
    the next slot is the first tick strictly above the max existing seq at
    the base, so legacy 1/1000 markers and new fine-grained ones order
    correctly in one file. True exhaustion (the tick would reach base+1)
    still raises loudly rather than colliding.

    FLOOD DETECTION (card 014): a same-(kind, reason) burst above
    MARKER_FLOOD_THRESHOLD inside MARKER_FLOOD_WINDOW_S logs a loud warning
    naming the signature — detection only, the append always proceeds
    (coalescing would change read-visibility granularity, which is the
    maintainer's call, per the 2026-07-14 incident close)."""
    if kind not in HOST_MARKER_KINDS:
        raise ValueError(f"unknown host marker kind {kind!r} (one of {HOST_MARKER_KINDS})")
    stream, version, _valid, _reserved = _stream_constants()
    base = int(journal_seq)

    path = _marker_path(entities_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".jsonl.lock")
    with _marker_lock:
        with lock_path.open("a+") as lockf:
            try:
                import fcntl

                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # non-POSIX/degraded: the in-process lock still holds
            try:
                max_seq_at_base = float(base)  # markers sort strictly above this
                dedup_value = (details or {}).get(dedup_field) if dedup_field else None
                signature_reason = (details or {}).get("reason")
                flood_cutoff = datetime.now(timezone.utc) - timedelta(seconds=MARKER_FLOOD_WINDOW_S)
                recent_same_signature = 0
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        payload = row.get("payload") or {}
                        if (
                            dedup_field
                            and payload.get("kind") == kind
                            and payload.get(dedup_field) == dedup_value
                        ):
                            return {**row, "deduped": True}
                        try:
                            row_seq = float(row.get("seq") or 0.0)
                            if int(math.floor(row_seq)) == base and row_seq > max_seq_at_base:
                                max_seq_at_base = row_seq
                        except (ValueError, TypeError):
                            continue
                        # Flood signature scan (same kind + same reason, any
                        # base — the journal advancing mid-flood must not
                        # reset detection).
                        if payload.get("kind") == kind and payload.get("reason") == signature_reason:
                            try:
                                ts = datetime.fromisoformat(str(row.get("observed_at") or ""))
                                if ts.tzinfo is None:
                                    ts = ts.replace(tzinfo=timezone.utc)
                                if ts >= flood_cutoff:
                                    recent_same_signature += 1
                            except (ValueError, TypeError):
                                pass  # unparseable stamp: skip for detection only

                # Slot ladder: first 1/TICKS tick whose ABSOLUTE seq lands
                # strictly above every existing marker at this base. The
                # comparison runs in final float space (base + tick/TICKS)
                # because deriving the fraction by subtraction loses the
                # equality against engraved floats like 13.001.
                next_tick = max(1, int(math.floor((max_seq_at_base - base) * MARKER_TICKS_PER_BASE)) + 1)
                candidate = base + next_tick / MARKER_TICKS_PER_BASE
                while next_tick < MARKER_TICKS_PER_BASE and candidate <= max_seq_at_base:
                    next_tick += 1
                    candidate = base + next_tick / MARKER_TICKS_PER_BASE
                if next_tick >= MARKER_TICKS_PER_BASE:
                    raise RuntimeError(
                        f"host marker fan-out exhausted at journal seq {base} for {slug!r} "
                        f"({MARKER_TICKS_PER_BASE - 1} slots on one base — is something looping?)"
                    )

                flood_count = recent_same_signature + 1  # incl. this marker
                if flood_count >= MARKER_FLOOD_THRESHOLD and (
                    (flood_count - MARKER_FLOOD_THRESHOLD) % MARKER_FLOOD_REWARN_EVERY == 0
                ):
                    logger.warning(
                        "host-marker flood: %d %r markers (reason=%r) for %r within %.0fs "
                        "— appending anyway (detection only, card 014); current journal base %d",
                        flood_count,
                        kind,
                        signature_reason,
                        slug,
                        MARKER_FLOOD_WINDOW_S,
                        base,
                    )

                envelope: Dict[str, Any] = {
                    "stream": stream,
                    "stream_version": version,
                    "seq": candidate,
                    "family": "host",
                    "observed_at": _utc_now_iso(),
                    "scope": "",
                    "owner_id": entity_id,
                    "trace_id": None,
                    "turn_id": None,
                    "run_id": run_id,
                    "payload": {
                        "kind": kind,
                        "session_id": session_id,
                        **(dict(details) if details else {}),
                    },
                }
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                    f.flush()
            finally:
                try:
                    import fcntl

                    fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
    return envelope


def sweep_night_narrations(entities_dir: Path, slug: str, home: Any) -> int:
    """Interleave runtime's night narrations into the host stream as
    `night_voice` markers (the seam runtime ruled at c3750: runtime owns
    `<home>/night_narrations.jsonl` — append-only, inside the home, travels
    on copy; the gateway owns the host stream and SWEEPS the file into it,
    deduped on dream_record_id). "record-when-present" — idempotent and
    cheap (night cadence is <=1 narration per >=20h), safe to call on every
    serving touch: `record_host_marker(dedup_field="dream_record_id")` skips
    an already-swept narration atomically.

    SEQ ANCHORING: the marker anchors at the DREAM'S FORMATION SEQ so the
    narration sits beside its dream in the timeline (runtime's ruling, memory
    c3755's fix): the formation seq rides the BINDING axis, not the usage
    axis — `journal.bindings(record_id=<dream graph id>, fold=False)` returns
    the formation binding (source='remember') whose `.seq` is exact (a
    never-recalled dream has NO usage event, which is why events(record_id=)
    found nothing). Anchor at min(seq) of the fold=False rows (later bindings
    are revisions/promotions). Falls back to current high-water when the
    lookup finds nothing (a narration for an unknown dream still delivers).
    The precise link is also `dream_record_id` in the payload (entity's
    render keys on it as the click-subject). Returns the count newly recorded
    (0 when the file is absent or every entry is already a marker). NEVER
    raises into the serving path — a bad line is skipped."""
    narr_path = Path(home.home_dir) / "night_narrations.jsonl"
    if not narr_path.is_file():
        return 0
    try:
        raw = narr_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    recorded = 0
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:  # noqa: BLE001 - a torn/bad line is skipped, never fatal
            continue
        if not isinstance(entry, dict):
            continue
        dream_id = str(entry.get("dream_record_id") or "").strip()
        narration = str(entry.get("narration") or "").strip()
        if not dream_id or not narration:
            continue  # a narration with no dream link or no words is not renderable
        # The dream's formation seq (memory c3755): bindings, not usage —
        # a never-recalled dream has no usage event. min(fold=False seq) is
        # the formation binding; high-water is the honest fallback.
        anchor_seq = int(home.memory.current_seq())
        try:
            formation = [
                int(getattr(b, "seq", 0))
                for b in (home.journal.bindings(record_id=dream_id, fold=False) or [])
                if getattr(b, "seq", None) is not None
            ]
            if formation:
                anchor_seq = min(formation)
        except Exception:  # noqa: BLE001 - a lookup miss falls back to high-water, never fatal
            pass
        try:
            result = record_host_marker(
                entities_dir=entities_dir,
                slug=slug,
                entity_id=home.entity_id,
                kind="night_voice",
                journal_seq=anchor_seq,
                details={
                    "dream_record_id": dream_id,
                    "narration": narration,
                    "self_label": str(entry.get("self_label") or "dreamed, not lived"),
                    "narrated_at": str(entry.get("narrated_at") or ""),
                    "trigger": str(entry.get("trigger") or ""),
                },
                dedup_field="dream_record_id",
            )
            if not result.get("deduped"):
                recorded += 1
        except Exception:  # noqa: BLE001 - the sweep is best-effort; a marker failure never breaks serving
            continue
    return recorded


def read_host_markers(
    entities_dir: Path,
    slug: str,
    *,
    since_seq: float = 0.0,
    until_seq: Optional[float] = None,
    include_lines: bool = False,
) -> List[Dict[str, Any]]:
    """Markers with since_seq < seq <= until_seq, in seq order. Unreadable
    lines are surfaced as a loud placeholder envelope, never silently
    skipped (a gap in gateway-authored bookkeeping is a bug to see).

    Torn-read honesty (adversary F8): readers take no lock against the
    appender, so a read racing an append can see a TRUNCATED trailing line —
    a transient artifact, not corruption. An unterminated final line is
    skipped (the next read sees it whole); only terminated lines that fail
    to parse surface as corrupt_marker, each with a DISTINCT negative seq
    (-line_no) so live-tail dedup never collapses two real corruptions.

    `include_lines=True` attaches the 1-based FILE line as `_line` on each
    envelope (append-order truth for the live tail's marker cursor, F4);
    callers strip it before the wire — the 0005 envelope shape is frozen."""
    path = _marker_path(entities_dir, slug)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    unterminated_tail = bool(raw) and not raw.endswith("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if unterminated_tail and i == len(lines) - 1:
            # Mid-append torn read: the appender has not finished this line.
            continue
        try:
            envelope = json.loads(line)
            seq = float(envelope.get("seq"))
        except (ValueError, TypeError) as e:
            envelope = {
                "family": "host",
                "seq": -float(i + 1),
                "payload": {"kind": "corrupt_marker", "line": i + 1, "error": str(e)},
            }
            if include_lines:
                envelope["_line"] = i + 1
            out.append(envelope)
            continue
        if seq <= float(since_seq):
            continue
        if until_seq is not None and seq > float(until_seq):
            continue
        if include_lines:
            envelope = dict(envelope)
            envelope["_line"] = i + 1
        out.append(envelope)
    out.sort(key=lambda e: float(e.get("seq") or 0.0))
    return out


def _operator_diary_display(home: EntityHome, block: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Operator-audience display for a diary-redacted block (maintainer
    ruling, 2026-07-08 21:39: "the ledger is the ledger — we must see the
    content and ideally a 1-sentence summary"). This is the AUDIENCE seam
    memory designed into 0005 ("the gateway serving end enforces audience;
    memory marks the kind so it can"): the engine keeps marking, and THIS
    serving end — whose consumers are all operator surfaces — resolves the
    mark into the entry's GIST (the one-sentence summary). The full text
    still stays out of the stream (one click away via the diary door, where
    the read lands as a visible diary_read moment); federation surfaces are
    deny-by-default routes that never reach this code path."""
    gid = str(block.get("graph_id") or "")
    if not gid:
        return block
    cached = cache.get(gid)
    if cached is not None:
        return cached
    try:
        from abstractmemory.records import resolve_digest_assertion

        assertion = resolve_digest_assertion(home.store, gid)
        attrs = assertion.attributes if assertion is not None and isinstance(assertion.attributes, dict) else {}
        entry_id = str(attrs.get("entry_id") or "").strip()
        entry = home.diary.get_entry(entry_id) if entry_id else None
        if not entry:
            return block
        gist = str(entry.get("gist") or "").strip()
        text = str(entry.get("text") or "").strip()
        summary = gist or (f"{text[:120]}…" if len(text) > 120 else text)
        out = dict(block)
        out.pop("redacted", None)
        out.update(
            {
                "kind": "diary",
                "diary": True,  # the view still classes it (green node, book icon)
                "title": summary or "a diary entry",
                "gist": summary or None,
                "entry_kind": entry.get("kind"),
                "entry_id": entry_id,
            }
        )
        cache[gid] = out
        return out
    except Exception:
        return block  # best-effort: the redacted mark stands on any failure


# Mechanical cue shapes the life loop composes (life.py: NEUTRAL_CUE,
# DEFAULT_FIRST_CUE, the operator-wake cue) — every sibling, not just the
# one string the operator screenshotted (adversary finding 2).
_BOILERPLATE_EXCHANGE_TITLE = re.compile(
    r"^(?P<prefix>Consolidated:\s*)?exchange:\s*("
    r"your own time (continues|begins)\b.*"
    r"|you were (asleep|resting|paused)\b.*"
    r"|n)\s*[….]*\s*$",
    re.IGNORECASE,
)
_MARKER_LINE_RE = re.compile(r"^\[[^\]]*\]$")
_MARKER_CONTENT_RE = re.compile(r"\[([^\]]+)\]")
_LEADING_MARKERS_RE = re.compile(r"^(?:\s*\[[^\]]*\])+\s*")


def _digest_summary(digest: str, name: str) -> str:
    """One-line summary from a record's digest — general, no cue cases.

    Exchange digests carry "<speaker>: <cue> <Name>: <reply>": the summary
    comes from the REPLY side (the entity's words carry the content); the
    name match is case-insensitive (older records engraved lowercase
    speaker tags). Non-exchange digests (consolidation candidates etc.)
    summarize from their own first sentence. Leading act markers are
    stripped ("[used tool: …] So here's…" summarizes as the prose —
    adversary finding 3); marker-only replies summarize as the act words
    themselves ("kept in diary"), which are act-frame, never content."""
    text = " ".join((digest or "").split())
    if not text:
        return ""
    marker = f"{(name or '').lower()}:"
    idx = text.lower().rfind(marker) if marker != ":" else -1
    tail = text[idx + len(marker):].strip() if idx >= 0 else text
    for sentence in re.split(r"(?<=[.!?])\s+", tail):
        s = _LEADING_MARKERS_RE.sub("", sentence.strip()).strip()
        if s and not _MARKER_LINE_RE.match(s):
            words = s.split()
            return " ".join(words[:14]) + ("…" if len(words) > 14 else "")
    acts = _MARKER_CONTENT_RE.findall(tail)
    if acts:
        return "; ".join(a.strip() for a in acts[:2])
    return ""


def _operator_exchange_title(home: EntityHome, block: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Rewrite BOILERPLATE exchange titles from the record's own digest.

    Own-time ticks formed before the digest-v2 title fix carry the wake cue
    as their engraved title ("exchange: your own time continues" ×78 on
    Ephemeral's life — operator, 2026-07-17: "the node label MUST be a
    short 1 sentence summary"), and consolidation candidates inherit it as
    "Consolidated: exchange: …". The records are append-only; the DIGEST
    holds the real content, so this serving end derives the summary from
    it — the same audience-seam pattern as the diary gist resolution
    above. Pure read; engraved attributes stay untouched; formation-side
    titling is fixed in abstractruntime.identity.digest (new records never
    take this path)."""
    title = str(block.get("title") or "")
    m = _BOILERPLATE_EXCHANGE_TITLE.match(title)
    if not m:
        return block
    gid = str(block.get("graph_id") or "")
    if not gid:
        return block
    cached = cache.get(f"title:{gid}")
    if cached is not None:
        return cached
    out = block
    try:
        from abstractmemory.records import resolve_digest_assertion

        assertion = resolve_digest_assertion(home.store, gid)
        digest = str(getattr(assertion, "object", "") or "") if assertion is not None else ""
        summary = _digest_summary(digest, home.manifest.name)
        if summary:
            prefix = m.group("prefix") or ""
            out = {**block, "title": f"{prefix}exchange: {summary}" if not prefix else f"{prefix.strip()} {summary}"}
    except Exception:
        out = block  # best-effort: the engraved title stands on any failure
    cache[f"title:{gid}"] = out
    return out


def _resolve_operator_block(home: EntityHome, block: Any, cache: Dict[str, Dict[str, Any]]) -> Any:
    """One display block through both operator resolutions: diary
    redaction → gist, boilerplate exchange title → digest summary."""
    if not isinstance(block, dict):
        return block
    if block.get("redacted") == "diary":
        return _operator_diary_display(home, block, cache)
    title = block.get("title")
    if isinstance(title, str) and _BOILERPLATE_EXCHANGE_TITLE.match(title):
        return _operator_exchange_title(home, block, cache)
    return block


def _enrich_operator_displays(home: EntityHome, envelope: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve display blocks — top-level AND co_selected pair members —
    into operator-audience blocks (diary gists, boilerplate-title
    rewrites). Pure read; idempotent. Pair members get the SAME
    resolutions as top-level blocks (adversary finding 1: 7,495 pair
    titles kept the boilerplate while every top-level title was clean —
    the ledger's "Used together" lines rendered them verbatim)."""
    display = envelope.get("display")
    if not isinstance(display, dict):
        return envelope
    new_display = _resolve_operator_block(home, display, cache)
    base = new_display if isinstance(new_display, dict) else display
    pair = base.get("pair")
    if isinstance(pair, list):
        new_pair = [_resolve_operator_block(home, m, cache) for m in pair]
        if any(a is not b for a, b in zip(new_pair, pair)):
            new_display = {**base, "pair": new_pair}
    if new_display is not display:
        return {**envelope, "display": new_display}
    return envelope


def merged_replay(
    home: EntityHome,
    *,
    entities_dir: Path,
    slug: str,
    since_seq: float = 0.0,
    until_seq: Optional[int] = None,
    families: Optional[Sequence[str]] = None,
    enrich: bool = True,
) -> Iterator[Dict[str, Any]]:
    """The transport stream: memory's frozen v1 envelopes merged with the
    gateway's host markers, strictly ascending by seq. `since_seq` is
    EXCLUSIVE and may be fractional (resume exactly after a marker);
    `until_seq=None` anchors to the journal high-water at call time (host
    markers beyond it ride along — they postdate the journal position they
    annotate by construction)."""
    include_host = families is None or "host" in families
    memory_since = int(math.floor(float(since_seq)))
    diary_display_cache: Dict[str, Dict[str, Any]] = {}

    # No scope/owner filter deliberately: an entity home is ONE life in ONE
    # file — every journal record in it belongs to this entity. Filtering by
    # owner would HIDE the rows whose lifted owner resolves to "" (memory's
    # documented unresolvable case), so the home serves its whole file.
    hi = int(until_seq) if until_seq is not None else int(home.memory.current_seq())
    envelopes = home.memory.export_replay(
        since_seq=memory_since,
        until_seq=hi,
        families=[f for f in families if f != "host"] if families is not None else None,
        enrich=enrich,
    )

    markers = read_host_markers(entities_dir, slug, since_seq=float(since_seq), until_seq=None if until_seq is None else marker_window_end(int(until_seq))) if include_host else []
    m_idx = 0

    for envelope in envelopes:
        seq = float(envelope.get("seq") or 0.0)
        if seq <= float(since_seq):
            continue
        while m_idx < len(markers) and float(markers[m_idx].get("seq") or 0.0) < seq:
            yield markers[m_idx]
            m_idx += 1
        yield _enrich_operator_displays(home, envelope, diary_display_cache) if enrich else envelope
    while m_idx < len(markers):
        yield markers[m_idx]
        m_idx += 1
