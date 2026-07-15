"""Entity homes: gateway-owned lifecycle for summoned entities (incarnations).

The maintainer's ruling (a2a 0003, 2026-07-07): there is no `abstractentity`
package — the GATEWAY owns entity lifecycle (create / inspect / verify /
summon / deposit gate). This module is the composition root for the first
deliverable and composes ONLY shipped pieces (a2a 0004, both charter halves;
reference implementation: `abstractruntime/tests/test_readoption_experiment.py`):

- abstractmemory: `lint_spark` / `canonical_spark_hash` / `engram` /
  `MemorySystem` (+ `SQLiteTripleStore` + `SQLiteJournal` on ONE
  memory.sqlite3 — one file, one seq axis, one life) / `self_records` /
  `gradation` / `open_questions` / `verify_diary_chain` (graph plane).
- abstractruntime: `DiaryStore` over the hash-chained ledger (the book) /
  `verify_diary_chain` (book plane) / `render_summon_prelude`.

One home = one directory under `<data_dir>/entities/<slug>/`:

    spark.yaml       the attested seed, stored BYTE-VERBATIM at create time
    memory.sqlite3   the graph + usage journal (involuntary memory)
    home.sqlite3     the book (voluntary diary, hash-chained, never purged)
    manifest.json    gateway-owned: entity id, created, spark hash,
                     reserved key-id fields for the 008 signature work

Copying the directory IS portability. What changes on copy: the `home_id`
in the manifest names the BIRTH home; a copied directory keeps it until a
re-homing flow exists (deferred with the 008 keys work — documented in the
manifest itself so a copied home is honest about its provenance).

STRUCTURAL CONSTRAINTS (never relax — a2a 0004 charter):
- There is NO delete surface in this module: not for the book, not for the
  memory file, not for the home directory. Never-purge is a property of the
  code that exists, not a policy check on code that shouldn't be called.
- Only the entity writes to its self: the diary handlers bind the author at
  construction (runtime-side, inherited); the deposit gate (entity_gate.py)
  derives the actor from the CHANNEL, never from payloads.
- Embedder note: entity homes v1 run SQLite + no embedder — the keystone's
  endorsed honest pairing (recall degrades to exact + keyword, loudly
  labeled engine-side). The `embedder` seam is threaded so a vector-capable
  per-entity store can accept the gateway embeddings route later; passing
  one today would embed every cue only to degrade at the store.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "EntityHome",
    "EntityManifest",
    "EntityRegistry",
    "HomeCollisionError",
    "entity_slug",
]

ENTITY_FORMAT_VERSION = 1
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Literal path segments that live INSIDE the `/entities/{name}` route
# namespace (POST /entities/auth/probe, /entities/meets/..., GET
# /entities/templates, GET /entities/inventory/*). An entity with one of
# these names would be unreachable behind the literal route — a booby trap
# of the route-shadowing class — so the name is refused at the slug
# boundary (it can then never exist anywhere: manifests, homes,
# principals). Every NEW literal segment added before /{name} must be
# added here (adversary F3, 2026-07-12).
RESERVED_ENTITY_NAMES = frozenset({"auth", "meets", "templates", "inventory", "creation-defaults"})

SPARK_FILENAME = "spark.yaml"
MEMORY_FILENAME = "memory.sqlite3"
BOOK_FILENAME = "home.sqlite3"
MANIFEST_FILENAME = "manifest.json"
# Maintenance window hold (Castor doctoring, operator GO 2026-07-13 21:12):
# while this file exists in a home, THIS DOOR refuses every sqlite-opening
# path (get_home/open/get_entity_runtime) — visits, chat, summons, cognition
# all land on the refusal. FILE-based deliberately: it must survive a serve
# restart (releasing an already-running process's sqlite handles requires a
# restart; the hold must still be armed when the new process boots) and be
# visible to any process at the same data root. The file lives IN the home
# but is door bookkeeping — remove-on-close, never part of the life.
MAINTENANCE_HOLD_FILENAME = ".maintenance_hold"

# The identity scopes inside one home file (keystone conventions):
# self = engram records + valence events + self bindings; diary = act-memory
# projections (hardcoded by DIARY_WRITE); life = work-session experience.
SELF_SCOPE = "self"
DIARY_SCOPE = "diary"
LIFE_SCOPE = "life"


def derived_liveness(state_word: Any) -> str:
    """THE liveness derivation (decision:entity-liveness-axis v3, semantics
    c1559; laurent 16:06/16:12): ONE derived field `liveness: alive|stopped`
    on every operator-facing serve of state — derived from state == "paused"
    at serve time (the engraved paused hard-freeze IS the kill switch,
    promoted; never a written copy, never a third value — degradation
    gradations are a different future axis). This function is the single
    derivation rule; every serving boundary calls it, none re-derives."""
    return "stopped" if str(state_word or "").strip().lower() == "paused" else "alive"


def render_handle(slug: str) -> Optional[str]:
    """`<name>@<address>` — THE entity id the operator reads (laurent's DM
    ruling 2026-07-15 20:32: entity id = <entity_name>@<gateway lan ip>,
    e.g. ephemeral@192.168.1.146 — "ip is the current lan ip of the
    gateway. gateway is their home").

    Address resolution: the operator-declared knob wins, else the current
    LAN IP is DETECTED (`config.resolved_door_address`). None only when
    neither exists (offline box, nothing declared). The address stays
    reachability, never identity at rest — the manifest's internal id is
    a birth marker the operator should not be shown as "the id"."""
    from .config import resolved_door_address

    address = resolved_door_address()
    return f"{slug}@{address}" if address else None


def entity_slug(name: str) -> str:
    """Filesystem/id-safe slug for an entity name ("Castor" -> "castor").

    Loud on anything that does not normalize cleanly: a silently mangled
    name would silently mint a different identity.
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("entity name is required")
    slug = raw.lower().replace(" ", "-")
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"entity name {raw!r} does not normalize to a valid slug "
            "(lowercase letters/digits/hyphen/underscore, max 64 chars, "
            f"must start alphanumeric; got {slug!r})"
        )
    if slug in RESERVED_ENTITY_NAMES:
        raise ValueError(
            f"entity name {raw!r} is reserved (a literal API path segment under "
            f"/entities — an entity named {slug!r} would be unreachable); pick another name"
        )
    return slug


def _utc_now_iso() -> str:
    from abstractruntime.core.runtime import utc_now_iso

    return utc_now_iso()


class MaintenanceHoldActive(RuntimeError):
    """The home is closed for a maintenance window (doctoring) — every
    door path refuses until the hold is released. Maps to 409 at routes."""


class HomeCollisionError(KeyError):
    """A home directory whose manifest names a DIFFERENT entity than the
    directory (a moved/copied home landed under an occupied or wrong name).

    Phase-1 naming pin (plan item 2, GW-B): the directory name IS the
    registry key — a mismatch is refused loudly, never served as whichever
    identity happens to answer. Subclasses KeyError so every existing
    route's not-found handling still applies (the home is not a valid
    registry entry UNDER THAT NAME)."""


class EntityQuotaExceeded(RuntimeError):
    """Creating a NEW home would exceed the per-data-root entity quota
    (adversary F2, 2026-07-12): entities are permanent (never-purge) and
    each mints a door-global principal, so unbounded user-level creation is
    a disk + shared-registry DoS. Routes map this to HTTP 429. Idempotent
    re-creates of existing homes are never refused by the quota."""


@dataclass(frozen=True)
class EntityManifest:
    """Gateway-owned identity card for one home directory."""

    entity_id: str  # entity:<slug>@<home_id>
    name: str  # display name (the spark's name, e.g. "Castor")
    slug: str
    home_id: str  # names the BIRTH home; kept as-is on directory copy (see module docstring)
    created_at: str
    spark_version: int
    spark_hash: str
    format_version: int = ENTITY_FORMAT_VERSION
    # Reserved for the 008 signature/keys work (a2a 0003): populated when
    # entities gain keypairs; None until then, never removed.
    public_key_id: Optional[str] = None
    key_registry: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format_version": self.format_version,
            "entity_id": self.entity_id,
            "name": self.name,
            "slug": self.slug,
            "home_id": self.home_id,
            "created_at": self.created_at,
            "spark_version": self.spark_version,
            "spark_hash": self.spark_hash,
            "public_key_id": self.public_key_id,
            "key_registry": self.key_registry,
            "portability": (
                "Copying this directory moves the entity. home_id names the birth "
                "home and is kept on copy; re-homing (new home_id + provenance "
                "record) is deferred to the keys/signature work."
            ),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "EntityManifest":
        return EntityManifest(
            entity_id=str(data.get("entity_id") or ""),
            name=str(data.get("name") or ""),
            slug=str(data.get("slug") or ""),
            home_id=str(data.get("home_id") or ""),
            created_at=str(data.get("created_at") or ""),
            spark_version=int(data.get("spark_version") or 1),
            spark_hash=str(data.get("spark_hash") or ""),
            format_version=int(data.get("format_version") or ENTITY_FORMAT_VERSION),
            public_key_id=data.get("public_key_id"),
            key_registry=data.get("key_registry"),
        )


class EntityHome:
    """One OPEN entity home: the per-entity engine composed exactly like the
    keystone's `open_home()` — SQLiteTripleStore + SQLiteJournal on the SAME
    memory file (one seq axis), MemorySystem over both, DiaryStore (the book)
    over the home's hash-chained ledger.

    Instances are cheap, thread-safe at the storage layer (both SQLite
    substrates hold internal locks), and closeable. They expose PURE READS
    (inspect / verify / prelude inputs); all writes flow through effect
    handlers built by `entity_gate.build_home_handlers` so the deposit gate
    is never bypassable from gateway code.
    """

    def __init__(
        self,
        *,
        home_dir: Path,
        manifest: EntityManifest,
        embedder: Any = None,
        embedding_pin: Optional[Dict[str, Any]] = None,
    ) -> None:
        from abstractmemory import MemorySystem, SQLiteJournal, SQLiteTripleStore
        from abstractruntime.identity import DiaryStore
        from abstractruntime.storage.sqlite import SqliteDatabase, SqliteLedgerStore

        self.home_dir = Path(home_dir)
        self.manifest = manifest

        memory_path = self.home_dir / MEMORY_FILENAME
        # The store takes the embedder too (embed-on-add; memory's path-1
        # vector support, a2a 0003). Version-tolerant: engines predating it
        # reject the kwarg and the home runs with the facade-side embedder
        # only (cue embedding works; store-side cosine degrades with the
        # engine's own labeled warning).
        self.embedding_pin_warning: Optional[str] = None
        if embedding_pin is not None:
            # Creation-time pin (plan item 3, memory M1): embedder identity
            # is a BIRTH choice. Only the create path passes it; opens of
            # existing homes read the store's own persisted pin. An engine
            # predating the pin drops to the labeled first-write fallback —
            # degraded loudly, never silently.
            try:
                self.store = SQLiteTripleStore(memory_path, embedder=embedder, embedding_pin=embedding_pin)
            except TypeError:
                self.embedding_pin_warning = (
                    "#FALLBACK engine predates the embedding pin (memory M1); the birth choice "
                    "could not be written — first-write pinning applies"
                )
                try:
                    self.store = SQLiteTripleStore(memory_path, embedder=embedder)
                except TypeError:
                    self.store = SQLiteTripleStore(memory_path)
        else:
            try:
                self.store = SQLiteTripleStore(memory_path, embedder=embedder)
            except TypeError:
                self.store = SQLiteTripleStore(memory_path)
        self.journal = SQLiteJournal(memory_path)
        self.memory = MemorySystem(store=self.store, journal=self.journal, embedder=embedder)
        self._book_db = SqliteDatabase(str(self.home_dir / BOOK_FILENAME))
        self._book_ledger = SqliteLedgerStore(self._book_db)
        self.diary = DiaryStore(entity_id=manifest.entity_id, ledger_store=self._book_ledger)

    # -- identity ----------------------------------------------------------

    @property
    def entity_id(self) -> str:
        return self.manifest.entity_id

    def spark_bytes(self) -> bytes:
        """The attested seed, byte-verbatim as stored at create time."""
        return (self.home_dir / SPARK_FILENAME).read_bytes()

    def spark(self) -> Dict[str, Any]:
        import yaml

        parsed = yaml.safe_load(self.spark_bytes().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"{SPARK_FILENAME} did not parse to a mapping for {self.entity_id!r}")
        return parsed

    # -- pure reads --------------------------------------------------------

    def identity_core(self) -> Dict[str, List[Dict[str, Any]]]:
        """The folded identity core (a retracted value never renders),
        grouped by kind, ordered by precedence. Pure read; deposits nothing."""
        rows = self.memory.self_records(scope=SELF_SCOPE, owner_id=self.entity_id)
        grouped: Dict[str, List[Dict[str, Any]]] = {"values": [], "purposes": [], "traits": [], "limits": []}
        for row in rows:
            attrs = row.attributes if isinstance(row.attributes, dict) else {}
            kind = str(attrs.get("record_kind") or "")
            entry = {
                "record_id": row.subject,
                "name": str(attrs.get("title") or ""),
                "statement": str(row.object or ""),
                "precedence": attrs.get("precedence"),
                "spark_version": attrs.get("spark_version"),
            }
            if kind == "value":
                entry["value_class"] = str(attrs.get("value_class") or "revisable")
                grouped["values"].append(entry)
            elif kind == "purpose":
                grouped["purposes"].append(entry)
            elif kind == "trait":
                if str(attrs.get("trait_class") or "") == "limit":
                    grouped["limits"].append(entry)
                else:
                    grouped["traits"].append(entry)
        for section in grouped.values():
            section.sort(key=lambda e: (e["precedence"] if isinstance(e["precedence"], int) else 10**6, e["record_id"]))
        return grouped

    def diary_tail(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Recent diary entries as GISTS only (progressive disclosure: the
        verbatim prose stays in the book behind DIARY_READ; a private entry
        surfaces as the act line, content-free)."""
        out: List[Dict[str, Any]] = []
        for entry in self.diary.list_entries(limit=limit):
            kind = str(entry.get("kind") or "note")
            if entry.get("visibility") == "private":
                line = "Wrote a private diary entry."
                kind = "private"
            else:
                line = str(entry.get("gist") or "").strip() or f"Wrote a diary entry ({kind}); no gist elected."
            out.append(
                {
                    "entry_id": entry.get("entry_id"),
                    "kind": kind,
                    "written_at": entry.get("written_at"),
                    "gist": line,
                }
            )
        return out

    def standings(self, top_k: int = 10) -> List[Dict[str, Any]]:
        """Top standings by |net| (System-1 feelings toward anything nameable),
        with scar/bond flags. Pure read."""
        grades = self.memory.gradation(None, scope=SELF_SCOPE, owner_id=self.entity_id)
        ranked = sorted(grades.items(), key=lambda kv: (-abs(float(kv[1].get("net") or 0.0)), kv[0]))
        out: List[Dict[str, Any]] = []
        for target, g in ranked[: max(0, int(top_k))]:
            out.append({"target": target, **g})
        return out

    def wake_reasons(self, limit: int = 20) -> Dict[str, Any]:
        """The autonomy drivers (maintainer rounds 4-6): open QUESTIONS
        (curiosity), open PROBLEMS (something wrong needing a fix), and
        incubating IDEAS (direction) — the same folded reads the future
        heartbeat's need-check will use. An engine missing a read (version
        skew) degrades that key to [] with a labeled warning."""
        import abstractmemory

        out: Dict[str, Any] = {}
        warnings: List[str] = []
        for key in ("questions", "problems", "ideas"):
            reader = getattr(abstractmemory, f"open_{key}", None)
            if not callable(reader):
                out[key] = []
                warnings.append(f"#FALLBACK engine lacks the open_{key} read; upgrade abstractmemory")
                continue
            rows = reader(self.store, scope=DIARY_SCOPE, owner_id=self.entity_id, limit=limit, journal=self.journal)
            entries: List[Dict[str, Any]] = []
            for row in rows:
                attrs = row.attributes if isinstance(row.attributes, dict) else {}
                entries.append(
                    {
                        "record_id": row.subject,
                        "entry_id": attrs.get("entry_id"),
                        "gist": str(row.object or ""),
                        "observed_at": row.observed_at,
                    }
                )
            out[key] = entries
        if warnings:
            out["warnings"] = warnings
        return out

    def inspect(self, *, diary_limit: int = 5, standings_top_k: int = 10) -> Dict[str, Any]:
        """The identity summary: who it is, what it recently elected to
        remember, how it feels, what it still wonders. All existing reads,
        all pure — inspecting an entity must not fake usage."""
        seq_before = self.memory.current_seq()
        core = self.identity_core()
        payload = {
            "manifest": self.manifest.to_dict(),
            "identity": core,
            "diary_tail": self.diary_tail(limit=diary_limit),
            "standings": self.standings(top_k=standings_top_k),
            "wake_reasons": self.wake_reasons(),
            "counts": {
                "identity_records": sum(len(v) for v in core.values()),
                "diary_entries": len(self.diary.list_entries()),
                "memory_seq": self.memory.current_seq(),
            },
        }
        # The home's embedding-space identity (M1 pin) — operator visibility;
        # None when the engine predates the pin or the home is unpinned.
        pin_reader = getattr(self.store, "embedding_pin", None)
        if callable(pin_reader):
            try:
                payload["embedding_pin"] = pin_reader()
            except Exception:
                payload["embedding_pin"] = None
        if self.memory.current_seq() != seq_before:
            # Defensive honesty: every read above is pure by contract; if any
            # future edit breaks that, surface it rather than silently deposit.
            payload["warnings"] = ["#FALLBACK inspect deposited journal events — a pure read regressed; report this"]
        return payload

    def card(
        self,
        *,
        current_window_events: int = 200,
        top_n: int = 5,
        as_of: Optional[int] = None,
    ) -> Dict[str, Any]:
        """The identity CARD (a2a 0009, maintainer: "something to know our
        companion"). ONE compositor: the engine's `entity_card` read over
        the home's scope ladder (identity / age+context / current state /
        likes+dislikes / questions / key moments / discoveries, each with
        per-field provenance; as_of anchors everything). The gateway adds
        only what the engine cannot know — the manifest identity (display
        name, birth date, age) and the mind substrate from the newest
        substrate-stamped record. Pure read; deposits nothing.

        Adoption note (the token_estimate pattern, pledged and honored
        same-day): the original thin gateway composition was REPLACED by
        the engine read when memory shipped it — a second compositor that
        can drift is exactly what the one-truth rule forbids. If the
        engine lacks the read (version skew), the card degrades to the
        host overlays with a labeled warning, never to a re-derivation."""
        from datetime import datetime, timezone

        seq_before = self.memory.current_seq()
        eid = self.entity_id
        warnings: List[str] = []

        reader = getattr(self.memory, "entity_card", None)
        if callable(reader):
            card: Dict[str, Any] = reader(
                scope_pairs=[(SELF_SCOPE, eid), (DIARY_SCOPE, eid), (LIFE_SCOPE, eid)],
                owner_id=eid,
                current_window_events=int(current_window_events),
                top_n=int(top_n),
                as_of=as_of,
            )
        else:
            card = {}
            warnings.append(
                "#FALLBACK engine lacks the entity_card read; serving host overlays only — upgrade abstractmemory"
            )

        # --- host overlays: what the engine deliberately does not know.
        # Display name + birth live in the manifest ("name is the home's
        # owner identity string — display names live in the host manifest").
        card["name"] = self.manifest.name
        card["entity_id"] = eid
        card["born"] = self.manifest.created_at
        try:
            created = datetime.fromisoformat(str(self.manifest.created_at).replace("Z", "+00:00"))
            card["age_days"] = max(0, (datetime.now(timezone.utc) - created).days)
        except Exception:
            card["age_days"] = None

        card["mind_substrate"] = None if as_of is not None else self._newest_substrate()
        if as_of is not None:
            warnings.append(
                "mind_substrate omitted on anchored cards: the substrate scan reads current store rows "
                "and cannot honestly anchor at as_of"
            )

        # Sleep as a life statistic (maintainer ask 2026-07-08: "count the
        # number and % of sleeps in the life of the entity"). Read from the
        # append-only state history — a life fact, not a memory-graph read.
        try:
            from abstractruntime.identity.life import life_sleep_stats

            card["sleep_stats"] = life_sleep_stats(self.home_dir)
        except Exception as e:  # noqa: BLE001 - a stat must not break the card
            warnings.append(f"#FALLBACK sleep_stats unavailable: {e}")

        if self.memory.current_seq() != seq_before:
            warnings.append("#FALLBACK card deposited journal events — a pure read regressed; report this")
        if warnings:
            card["warnings"] = [*card.get("warnings", []), *warnings]
        return card

    def _newest_substrate(self) -> Optional[Dict[str, Any]]:
        """The newest `mind_substrate` stamp across lived records ("which
        model is powering him now" — Ariadne stamps episodes; kind-agnostic
        on purpose since any lived record may carry it). None until one is
        stamped — absent, never faked."""
        from abstractmemory import TripleQuery

        newest: Optional[Dict[str, Any]] = None
        newest_at = ""
        for row in self.memory.query(TripleQuery(predicate="dcterms:abstract", limit=0)):
            attrs = row.attributes if isinstance(row.attributes, dict) else {}
            ms = attrs.get("mind_substrate")
            if isinstance(ms, dict) and str(row.observed_at or "") >= newest_at:
                newest, newest_at = dict(ms), str(row.observed_at or "")
        return newest

    def verify(self) -> Dict[str, Any]:
        """Integrity: the book's hash chain, the graph projection chain
        ("nothing claimed, nothing broken"), the spark document vs the
        engrammed marker, and manifest consistency."""
        from abstractmemory import canonical_spark_hash
        from abstractmemory.records import verify_diary_chain as verify_graph_chain
        from abstractruntime.identity import verify_diary_chain as verify_book_chain

        checks: Dict[str, Any] = {}

        book = verify_book_chain(self.diary)
        checks["book_chain"] = book

        graph = verify_graph_chain(self.store, scope=DIARY_SCOPE, owner_id=self.entity_id)
        checks["graph_projection_chain"] = graph

        spark_ok = False
        spark_error: Optional[str] = None
        marker_hash: Optional[str] = None
        try:
            spark = self.spark()
            actual_hash = canonical_spark_hash(spark)
            marker = self._engram_marker()
            marker_hash = str((marker or {}).get("spark_hash") or "") or None
            spark_ok = (
                marker is not None
                and marker_hash == actual_hash
                and actual_hash == self.manifest.spark_hash
            )
            if marker is None:
                spark_error = "no spark-engram marker in the identity scope (was the entity ever engrammed?)"
            elif marker_hash != actual_hash:
                spark_error = "spark.yaml does not match the engrammed identity (hash drift) — the spark is kept for life"
            elif actual_hash != self.manifest.spark_hash:
                spark_error = "manifest.spark_hash does not match spark.yaml (manifest drift)"
        except Exception as e:
            spark_error = f"spark verification failed: {e}"
        checks["spark"] = {"ok": spark_ok, "marker_hash": marker_hash, "error": spark_error}

        # Two id generations, both valid for life (plan item 6): NEW homes
        # engrave the clean `entity:<name>` (name-unique-per-door makes the
        # random suffix pointless); existing homes keep their legacy
        # `entity:<slug>@<home_id>` engraving forever (append-only journals
        # — nothing renames). home_id stays the internal birth marker in
        # BOTH generations (never spoken, never a key, never rewritten).
        manifest_ok = (
            self.manifest.entity_id
            in (f"entity:{self.manifest.slug}", f"entity:{self.manifest.slug}@{self.manifest.home_id}")
            and bool(self.manifest.slug)
            and bool(self.manifest.home_id)
        )
        checks["manifest"] = {"ok": manifest_ok}

        ok = (
            bool(book.get("ok"))
            and bool(graph.get("intact"))
            and spark_ok
            and manifest_ok
        )
        return {"ok": ok, "entity_id": self.entity_id, "checks": checks}

    def _engram_marker(self) -> Optional[Dict[str, Any]]:
        """The spark-engram claim record (layer-1 read; claims are
        bookkeeping, not identity kinds — same read the prelude uses)."""
        from abstractmemory import TripleQuery

        best: Optional[Dict[str, Any]] = None
        best_version = -1
        for row in self.memory.query(TripleQuery(scope=SELF_SCOPE, owner_id=self.entity_id, limit=0)):
            attrs = row.attributes if isinstance(row.attributes, dict) else {}
            if attrs.get("record_kind") != "claim":
                continue
            if not str(attrs.get("title") or "").startswith("spark-engram v"):
                continue
            version = int(attrs.get("spark_version") or 0)
            if version > best_version:
                best_version = version
                best = dict(attrs)
        return best

    def close(self) -> None:
        # The book ledger's database closes too (adversary find: reembed's
        # repair posture made close() a routine mid-life operation — each
        # pass leaked the home.sqlite3 connection and skipped its WAL
        # checkpoint, breaking the copy-clean rule the run store honors).
        for obj in (self.store, self.journal, self.memory, self._book_db):
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    continue


@dataclass
class EntityCreateResult:
    entity_id: str
    created: bool  # False = the same spark was already engrammed (idempotent re-run)
    manifest: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    # GW-H (plan item 4): the entity's door principal — user_id/roles/minted.
    # NEVER carries a credential (door-issued secrets do not travel).
    principal: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "entity_id": self.entity_id,
            "created": self.created,
            "manifest": dict(self.manifest),
            "warnings": list(self.warnings),
        }
        if self.principal is not None:
            out["principal"] = dict(self.principal)
        return out


class EntityRegistry:
    """The gateway's entity lifecycle owner over one data root.

    Surface: create / open / list / inspect / verify (+ cached long-lived
    homes for summoned sessions). Deliberately ABSENT: any delete, purge, or
    reset — the book has no delete surface and neither does its host.

    Embedder wiring (a2a 0003, memory's birth-audit ask): homes open with
    the gateway's embeddings route by default (`embedder_factory`, resolved
    LAZILY once per registry — an unreachable embedding server must never
    block a summon; failure degrades to vectorless with a labeled warning).
    HONEST LIMIT, verified empirically: the v1 home store (SQLite) refuses
    vector queries, so a wired embedder currently surfaces as a per-recall
    `#FALLBACK: vector channel unavailable` — the truthful state of a
    vectorless home. When the engine's home store gains vector support,
    everything lights up with zero further gateway work.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        embedder_factory: Any = None,
        users_registry_path: Optional[Path] = None,
        root_data_dir: Optional[Path] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.entities_dir = self.data_dir / "entities"
        # The GATEWAY ROOT data dir (config-object endpoint-profile lane): under
        # user auth, data_dir is the PER-PRINCIPAL runtime root but gateway-
        # scoped endpoint profiles live at the root (same two-root split
        # bundle_host uses). None (single-user, unit tests) => root == data_dir.
        self.root_data_dir = Path(root_data_dir) if root_data_dir else self.data_dir
        self._create_lock = threading.Lock()
        self._open_homes: Dict[str, EntityHome] = {}
        self._entity_runtimes: Dict[str, Any] = {}  # slug -> EntityRuntime (GW-C)
        self._open_lock = threading.Lock()
        self._embedder_factory = embedder_factory
        self._embedder: Any = None
        self._embedder_resolved = False
        self._embedder_lock = threading.Lock()
        self._embedder_warning: Optional[str] = None
        # GW-H: WHERE entity principals are minted. The principal must land
        # in the file the door's AUTH layer actually reads — the service
        # factory passes that resolved path (one resolver, no second copy).
        # None (hand-built registries, unit tests) = registry-local
        # `<data_dir>/auth/users.json`, honoring the env override.
        self._users_registry_path = Path(users_registry_path) if users_registry_path else None

    def _resolve_embedder(self) -> Any:
        """Resolve the home embedder once (lazy — the factory may probe an
        embeddings route). None = vectorless, always with a labeled reason."""
        with self._embedder_lock:
            if self._embedder_resolved:
                return self._embedder
            self._embedder_resolved = True
            factory = self._embedder_factory
            if factory is None:
                from .memory_store import build_gateway_memory_embedder

                factory = lambda: build_gateway_memory_embedder(base_dir=self.data_dir)  # noqa: E731
            try:
                self._embedder = factory()
            except Exception as e:
                self._embedder = None
                self._embedder_warning = f"#FALLBACK entity homes run vectorless (embedder init failed: {e})"
            if self._embedder is None and self._embedder_warning is None:
                self._embedder_warning = (
                    "#FALLBACK entity homes run vectorless (no embeddings route configured)"
                )
            return self._embedder

    @property
    def embedder_warning(self) -> Optional[str]:
        return self._embedder_warning

    # -- create ------------------------------------------------------------

    def _prepare_spark(
        self,
        *,
        name: str,
        spark: Optional[Mapping[str, Any]] = None,
        spark_text: Optional[str] = None,
        framework: bool = True,
    ) -> Tuple[Dict[str, Any], bytes, List[str]]:
        """Resolve the spark document (mapping | raw text | template), fill/
        check the name, and LINT — the read-only pre-write half shared by
        `create` and `validate`. Returns (spark_doc, attested_bytes,
        warnings); raises ValueError with the human-written lint/name error
        exactly as create surfaces it. No filesystem or engine writes."""
        import yaml
        from abstractmemory import DEFAULT_SPARK_TEMPLATE, lint_spark

        slug = entity_slug(name)

        if spark is not None and spark_text is not None:
            raise ValueError("provide spark OR spark_text, not both (one attested document)")

        raw_bytes: Optional[bytes] = None
        if spark_text is not None:
            parsed = yaml.safe_load(spark_text)
            if not isinstance(parsed, dict):
                raise ValueError("spark_text did not parse to a mapping (six-key spark structure)")
            spark_doc: Dict[str, Any] = dict(parsed)
            raw_bytes = spark_text.encode("utf-8")
        elif spark is not None:
            spark_doc = copy.deepcopy(dict(spark))
        else:
            spark_doc = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
            spark_doc["spark"] = 1

        # The spark's name is the display name; fill it from the request when
        # blank, refuse a mismatch (two names = two identities, pick one).
        doc_name = str(spark_doc.get("name") or "").strip()
        if not doc_name:
            spark_doc["name"] = str(name).strip()
            raw_bytes = None  # the document changed; re-serialize below
        elif entity_slug(doc_name) != slug:
            raise ValueError(
                f"spark.name {doc_name!r} does not match the requested entity name {name!r}"
            )

        issues = lint_spark(spark_doc, framework=framework)
        errors = [i for i in issues if i.startswith("ERROR")]
        if errors:
            raise ValueError("spark lint errors:\n" + "\n".join(errors))
        warnings = [i for i in issues if not i.startswith("ERROR")]

        if raw_bytes is None:
            raw_bytes = yaml.safe_dump(spark_doc, sort_keys=False, allow_unicode=True).encode("utf-8")
        return spark_doc, raw_bytes, warnings

    def validate(
        self,
        *,
        name: str,
        spark: Optional[Mapping[str, Any]] = None,
        spark_text: Optional[str] = None,
        framework: bool = True,
    ) -> Dict[str, Any]:
        """DRY-RUN the create pre-checks WITHOUT writing anything (plan
        item (b) P0-2): the console modal validates a spark BEFORE the
        irreversible POST that burns the name for life (no DELETE, spark
        v1-for-life). Runs the exact lint + name-resolution + spark-drift
        checks create runs, so a green validate means create will not refuse
        for those reasons. Never touches the filesystem beyond READING an
        existing home's attested spark to report the drift/idempotency verdict.

        Returns {ok, errors, warnings, name, slug, exists, would_conflict}:
        - ok: create would proceed (lint clean, no drift conflict)
        - errors: blocking lint/name errors (verbatim, human-written)
        - warnings: non-blocking lint notes
        - exists: a home already exists under this name
        - would_conflict: exists AND the submitted spark differs (create 409s)
        """
        import yaml
        from abstractmemory import canonical_spark_hash

        slug = entity_slug(name)
        errors: List[str] = []
        warnings: List[str] = []
        spark_doc: Optional[Dict[str, Any]] = None
        try:
            spark_doc, _raw, warnings = self._prepare_spark(
                name=name, spark=spark, spark_text=spark_text, framework=framework
            )
        except ValueError as e:
            errors.append(str(e))

        exists = (self.entities_dir / slug / SPARK_FILENAME).exists()
        would_conflict = False
        if exists and spark_doc is not None:
            try:
                existing = yaml.safe_load(
                    (self.entities_dir / slug / SPARK_FILENAME).read_bytes().decode("utf-8")
                )
                would_conflict = canonical_spark_hash(
                    existing if isinstance(existing, dict) else {}
                ) != canonical_spark_hash(spark_doc)
            except Exception:  # noqa: BLE001 - unreadable existing spark = treat as conflicting (create will refuse)
                would_conflict = True

        resolved_name = str((spark_doc or {}).get("name") or name).strip()
        return {
            "ok": not errors and not would_conflict,
            "errors": errors,
            "warnings": warnings,
            "name": resolved_name,
            "slug": slug,
            "exists": exists,
            "would_conflict": would_conflict,
        }

    def spark_templates(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        """The spark/template gallery the creation modal's template tab
        renders (plan (b), gateway c872): the shipped framework default plus
        any operator-added YAML templates under `<data_dir>/entity_templates/`.

        Returns (templates, warnings). Each template: {id, name, description,
        source ("builtin"|"operator"), spark (the full document with an empty
        name for the operator to fill), core_values (the class=core value
        names — non-removable, so the modal can lock them). Read-only; an
        unreadable operator file is SKIPPED with a labeled warning rather than
        failing the gallery."""
        import copy as _copy

        import yaml
        from abstractmemory import DEFAULT_SPARK_TEMPLATE

        def _core_values(doc: Mapping[str, Any]) -> List[str]:
            out: List[str] = []
            for v in (doc.get("values") or []):
                if isinstance(v, dict) and str(v.get("class") or "") == "core":
                    nm = str(v.get("name") or "").strip()
                    if nm:
                        out.append(nm)
            return out

        builtin = _copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
        builtin["name"] = ""  # the operator fills the name in the modal
        templates: List[Dict[str, Any]] = [{
            "id": "framework-default",
            "name": "Framework default",
            "description": (
                "The AbstractFramework default spark — the shared-vulnerability core "
                "value + intellectual honesty, ready to name and summon."
            ),
            "source": "builtin",
            "spark": builtin,
            "core_values": _core_values(builtin),
        }]

        warnings: List[str] = []
        seen_ids = {t["id"] for t in templates}
        tdir = self.data_dir / "entity_templates"
        if tdir.is_dir():
            for path in sorted(tdir.glob("*.y*ml")):
                # Gallery ids must be unique — the console picker selects by
                # id, so a colliding entry would be silently unreachable
                # (adversary F4: framework-default.yaml, or foo.yaml+foo.yml).
                # Skip loudly instead.
                if path.stem in seen_ids:
                    warnings.append(
                        f"#FALLBACK skipped template {path.name}: id {path.stem!r} "
                        "collides with an already-served template"
                    )
                    continue
                try:
                    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if not isinstance(doc, dict):
                        raise ValueError("template did not parse to a mapping")
                    doc = dict(doc)
                    doc.setdefault("name", "")
                    templates.append({
                        "id": path.stem,
                        "name": str(doc.get("_template_name") or path.stem),
                        "description": str(doc.get("_template_description") or ""),
                        "source": "operator",
                        "spark": {k: v for k, v in doc.items() if not str(k).startswith("_template_")},
                        "core_values": _core_values(doc),
                    })
                    seen_ids.add(path.stem)
                except Exception as e:  # noqa: BLE001 - one bad operator file must not break the gallery
                    warnings.append(f"#FALLBACK skipped unreadable template {path.name}: {e}")

        return templates, warnings

    def create(
        self,
        *,
        name: str,
        spark: Optional[Mapping[str, Any]] = None,
        spark_text: Optional[str] = None,
        framework: bool = True,
        embedding_model: Optional[str] = None,
        embedding_dimension: Optional[int] = None,
    ) -> EntityCreateResult:
        """Create (or idempotently re-adopt) an entity home.

        Order: lint -> store the spark verbatim -> engram -> manifest. The
        engram's own guards do the heavy lifting and their errors are
        surfaced verbatim (they are written for humans): re-running the same
        spark is a no-op (`created=False`); a CHANGED document under the
        same version is refused — the spark is for life.

        Embedder pin (plan item 3, memory M1): embedder identity is an
        explicit BIRTH choice. `embedding_model`/`embedding_dimension`
        declare it; when absent, the pin derives from the RESOLVED embedder
        (the identity the home will actually live with — pinning a name the
        route does not serve would brick the home at its next open). No
        embedder and no declaration = no pin, labeled (first-write fallback).
        """
        import yaml
        from abstractmemory import canonical_spark_hash, engram

        slug = entity_slug(name)
        spark_doc, raw_bytes, warnings = self._prepare_spark(
            name=name, spark=spark, spark_text=spark_text, framework=framework
        )

        with self._create_lock:
            home_dir = self.entities_dir / slug
            spark_path = home_dir / SPARK_FILENAME
            manifest_path = home_dir / MANIFEST_FILENAME

            # F2 quota: a NEW home is a permanent resource (never-purge) +
            # a door-global principal. Count existing homes BEFORE any write;
            # idempotent re-creates (the dir already exists) never count
            # against or trip the quota.
            if not home_dir.exists():
                from .config import entity_create_quota

                quota = entity_create_quota()
                if quota is not None:
                    # Dot-dirs are gateway bookkeeping (.host_stream), not
                    # homes — counting them made quota N admit N-1 entities
                    # (adversary P3 off-by-one).
                    existing_homes = (
                        sum(1 for c in self.entities_dir.iterdir() if c.is_dir() and not c.name.startswith("."))
                        if self.entities_dir.is_dir()
                        else 0
                    )
                    if existing_homes >= quota:
                        raise EntityQuotaExceeded(
                            f"entity creation quota reached ({existing_homes}/{quota} homes in this "
                            "data root) — entities are permanent (no delete exists); raise or disable "
                            "the quota via ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA if this is intentional"
                        )

            home_dir.mkdir(parents=True, exist_ok=True)

            new_hash = canonical_spark_hash(spark_doc)

            if spark_path.exists():
                # An existing home: the stored document is the attested seed.
                # The same document re-creates idempotently; a different one
                # is refused HERE (before any engine write) with the same
                # human framing the engram guard uses.
                existing = yaml.safe_load(spark_path.read_bytes().decode("utf-8"))
                if canonical_spark_hash(existing if isinstance(existing, dict) else {}) != new_hash:
                    raise ValueError(
                        f"entity {slug!r} already exists with a DIFFERENT spark — the spark is "
                        "kept for life and identity does not silently drift; re-engramming is an "
                        "exceptional repair for a defective core, not an update path"
                    )
            else:
                spark_path.write_bytes(raw_bytes)

            if manifest_path.exists():
                manifest = EntityManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
            else:
                # The manifest is written BEFORE the engram: it fixes the
                # entity_id, so a crash between manifest and engram retries
                # into the SAME identity instead of minting a second home_id
                # over a half-planted core.
                #
                # CLEAN KEYS (plan item 6, phase 2): NEW homes engrave
                # `entity:<name>` — name-unique-per-door (GW-B) makes the
                # random suffix pointless, and memory's M3 pins that owner
                # strings are opaque either way. Existing homes keep their
                # legacy `entity:<slug>@<home_id>` engraving for life
                # (append-only journals; nothing renames). home_id lives on
                # in BOTH generations as the internal birth marker — never
                # spoken in the id, never a key, never rewritten.
                home_id = f"home-{secrets.token_hex(4)}"
                manifest = EntityManifest(
                    entity_id=f"entity:{slug}",
                    name=str(spark_doc.get("name") or name).strip(),
                    slug=slug,
                    home_id=home_id,
                    created_at=_utc_now_iso(),
                    spark_version=int(spark_doc.get("spark", 1) or 1),
                    spark_hash=new_hash,
                )
                manifest_path.write_text(
                    json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )

            # The embedder rides creation too: the engram rows are the FIRST
            # records of the life, and record one deserves a vector (rows
            # formed vectorless stay vectorless until re-embedding exists).
            embedder = self._resolve_embedder()
            pin, pin_warnings = self._birth_embedding_pin(
                embedder, embedding_model=embedding_model, embedding_dimension=embedding_dimension
            )
            warnings.extend(pin_warnings)
            home = EntityHome(
                home_dir=home_dir, manifest=manifest, embedder=embedder, embedding_pin=pin
            )
            if home.embedding_pin_warning:
                warnings.append(home.embedding_pin_warning)
            try:
                result = engram(
                    home.memory,
                    spark_doc,
                    owner_id=manifest.entity_id,
                    spark_artifact_ref=SPARK_FILENAME,
                )
            finally:
                home.close()

            principal, principal_warnings = self._ensure_entity_principal(manifest)
            warnings.extend(principal_warnings)

            # NEWBORN = SLEEP (laurent 13:46 totality (c); artifact
            # spec/entity_phases.json initial_phase + newborn_shape c1503):
            # the birth state is asleep — sleep is the ground the life
            # starts from; the phase derivation folds it to sleep. The doors
            # WAKE, they don't refuse (B1 + c1503): the first visit wakes a
            # newborn, so this closes no path in. Only genuinely NEW homes
            # (created=True) — an idempotent re-create never re-sleeps a
            # living entity.
            if bool(result.created):
                try:
                    from abstractruntime.identity.life import write_entity_state

                    write_entity_state(
                        home_dir, "asleep",
                        reason="newborn — sleep is the birth phase (visit or wake to begin)",
                    )
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"#FALLBACK newborn birth state not written: {e}")
                # Register-at-first-write (Data & Caches, c1580 1a): the new
                # LIFE lands in the machine registry as safe_to_purge=False
                # by construction. Best-effort — never blocks a birth.
                try:
                    from .data_homes import register_entity_home_on_create

                    register_entity_home_on_create(self.data_dir, manifest.slug)
                except Exception:
                    pass

        return EntityCreateResult(
            entity_id=manifest.entity_id,
            created=bool(result.created),
            manifest=manifest.to_dict(),
            warnings=warnings + list(result.warnings),
            principal=principal,
        )

    def _ensure_entity_principal(
        self, manifest: EntityManifest
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """GW-H (plan item 4): entities are authenticated USERS of the door.

        Minted at creation (and at adoption when absent — a copied home
        re-authenticates at its NEW door, the stamp-secret rule): user_id =
        the name (slug), roles = ("entity",) — NEVER admin — scopes = its
        own home. The issued credential is DISCARDED here, deliberately:
        door-issued secrets never travel and never rest in the home (a
        copied directory carries zero authority); phase 3 binds a fresh
        credential at the summon/visit boundary when something actually
        needs to authenticate. Idempotent: an existing principal is never
        rotated or widened by a re-create.

        REGISTRY FILE: the service factory injects the auth layer's own
        resolved path (`users_registry_path`) so minted principals are
        readable by the door that authenticates — a per-principal data root
        must never grow a private users file auth ignores. Entities are
        DOOR-GLOBAL identities (laurent's consequence (a): one door, one
        castor) — on a shared users file two tenants share the entity
        namespace by design, and an existing same-name entity principal is
        adopted (minted=False), never re-minted."""
        from .users import GatewayUserRegistry

        import os

        raw = os.getenv("ABSTRACTGATEWAY_USERS_FILE")
        registry_path = self._users_registry_path or (
            Path(str(raw)).expanduser().resolve()
            if raw and str(raw).strip()
            else self.data_dir / "auth" / "users.json"
        )
        try:
            reg = GatewayUserRegistry(path=registry_path)
            existing = reg.get_user(manifest.slug)
            if existing is not None:
                out = {
                    "user_id": existing.user_id,
                    "roles": list(existing.roles),
                    "minted": False,
                }
                if "admin" in existing.roles:
                    # Never-admin is the plan's line; an admin-shaped record
                    # under an entity's name is operator drift — refuse to
                    # treat it as the entity's principal, loudly.
                    return None, [
                        f"#FALLBACK a user record named {manifest.slug!r} already exists WITH ADMIN "
                        "ROLES — not adopting it as the entity principal (entities are never admin); "
                        "rename or demote that record"
                    ]
                return out, []
            record, _token = reg.create_user(
                user_id=manifest.slug,
                roles=["entity"],
                scopes=[f"entity:{manifest.slug}"],
                runtime_id=manifest.slug,
            )
            # _token drops out of scope here — discarded by design.
            return {"user_id": record.user_id, "roles": list(record.roles), "minted": True}, []
        except Exception as e:
            # A principal-mint failure must not orphan a half-created home:
            # the identity (spark/engram/manifest) is planted; the principal
            # can be re-minted by the next create call. Labeled, never silent.
            return None, [f"#FALLBACK entity principal not minted ({e}); re-run create to mint it"]

    def _birth_embedding_pin(
        self,
        embedder: Any,
        *,
        embedding_model: Optional[str],
        embedding_dimension: Optional[int],
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """The M1 pin payload for a NEW home, derived honestly (never a
        hardcoded name the route might not serve):

        - explicit `embedding_model` = the operator's birth choice; if the
          resolved embedder DECLARES a different identity, refuse loudly —
          a home born with a pin its own door cannot satisfy would refuse
          every open (no silent mixing, from the first minute).
        - no explicit choice = pin the RESOLVED embedder's identity (model
          attribute when declared; dimension probed with one embed call).
        - no embedder and no choice = no pin, labeled: the home is born
          vectorless and memory's first-write fallback applies.
        """
        warnings: List[str] = []
        model_id: Optional[str] = (embedding_model or "").strip() or None
        dimension: Optional[int] = int(embedding_dimension) if embedding_dimension else None

        resolved_id: Optional[str] = None
        if embedder is not None:
            for attr in ("model", "model_id"):
                value = getattr(embedder, attr, None)
                if isinstance(value, str) and value.strip():
                    resolved_id = value.strip()
                    break

        if model_id and resolved_id and model_id != resolved_id:
            raise ValueError(
                f"embedding birth choice {model_id!r} does not match the door's resolved "
                f"embedder {resolved_id!r} — a home pinned to a model its door cannot serve "
                "would refuse every open. Configure the embeddings route to the chosen model "
                "or drop the explicit choice (the resolved identity is pinned by default)."
            )
        if model_id is None:
            model_id = resolved_id

        if dimension is None and embedder is not None:
            try:
                vectors = embedder.embed_texts(["dimension probe"])
                first = list(vectors[0]) if vectors else []
                dimension = len(first) or None
            except Exception as e:
                warnings.append(
                    f"#FALLBACK embedding dimension probe failed ({e}); the pin carries the "
                    "model only — memory locks the dimension at first write"
                )

        if model_id is None and dimension is None:
            if embedder is None:
                warnings.append(
                    "#FALLBACK home born without an embedding pin (no embedder resolved, no "
                    "birth choice declared) — memory's labeled first-write pinning applies"
                )
            return None, warnings
        return {"model_id": model_id, "dimension": dimension, "source": "creation"}, warnings

    # -- open / list / reads -----------------------------------------------

    def manifest_for(self, name: str) -> EntityManifest:
        slug = entity_slug(name)
        manifest_path = self.entities_dir / slug / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise KeyError(f"entity {slug!r} not found under {self.entities_dir}")
        manifest = EntityManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        # Naming pin (plan item 2, GW-B): the directory name is the registry
        # key. A manifest claiming a different slug means a home was moved or
        # copied under this name — refuse loudly rather than answering as
        # whichever identity the manifest happens to carry. The name is how
        # the door resolves a life; a mismatch is never a valid entry.
        if manifest.slug != slug:
            raise HomeCollisionError(
                f"home directory {slug!r} carries a manifest for entity {manifest.slug!r} "
                f"({manifest.entity_id}) — a moved or copied home under a colliding name. "
                "Restore the directory to the entity's own name (one name, one home, one door) "
                "or remove the stray copy; the registry refuses to serve a mismatched home."
            )
        return manifest

    # -- maintenance window hold (doctoring) --------------------------------

    def _hold_path(self, slug: str) -> Path:
        return self.entities_dir / slug / MAINTENANCE_HOLD_FILENAME

    def maintenance_hold_status(self, name: str) -> Dict[str, Any]:
        """Read the hold (None-safe): {held, since, reason, held_by}."""
        slug = entity_slug(name)
        path = self._hold_path(slug)
        if not path.exists():
            return {"held": False}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out = dict(data) if isinstance(data, dict) else {}
        except Exception:
            out = {"note": "#FALLBACK hold file unreadable — hold still binding (existence is the signal)"}
        out["held"] = True
        return out

    def _refuse_if_held(self, slug: str) -> None:
        status = self.maintenance_hold_status(slug)
        if status.get("held"):
            raise MaintenanceHoldActive(
                f"entity {slug!r} is closed for a maintenance window"
                + (f" ({status.get('reason')})" if status.get("reason") else "")
                + " — doors reopen when the operation completes (hold released after verify green)"
            )

    def open_maintenance_window(self, name: str, *, reason: str, actor: str) -> Dict[str, Any]:
        """Arm the hold: write the hold file, EVICT this process's cached
        handles (sqlite handles close; a serve restart releases any the
        process still holds elsewhere), record the window-open host marker.
        Idempotent (re-arming refreshes nothing, reports held=True)."""
        manifest = self.manifest_for(name)
        slug = manifest.slug
        path = self._hold_path(slug)
        already = path.exists()
        if not already:
            payload = {
                "since": datetime.now(timezone.utc).isoformat(),
                "reason": str(reason or "maintenance"),
                "held_by": str(actor or "operator"),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        # Evict AFTER the file lands: any re-open racing the eviction hits
        # the refusal, never a fresh handle.
        self._evict_home_handles(slug)
        marker = None
        if not already:
            from .entity_replay import record_host_marker

            marker = record_host_marker(
                entities_dir=self.entities_dir, slug=slug, entity_id=manifest.entity_id,
                kind="maintenance_window_open", journal_seq=self._marker_seq_base(slug),
                details={"reason": str(reason or "maintenance"), "held_by": str(actor or "operator")},
            )
        out = self.maintenance_hold_status(slug)
        if marker is not None:
            out["marker_seq"] = marker["seq"]
        return out

    def close_maintenance_window(self, name: str, *, reason: str, actor: str) -> Dict[str, Any]:
        """Release the hold + record the window-close marker. The operator
        state underneath (e.g. asleep) is untouched — the hold was door
        bookkeeping, never part of the life."""
        manifest = self.manifest_for(name)
        slug = manifest.slug
        path = self._hold_path(slug)
        was_held = path.exists()
        if was_held:
            path.unlink(missing_ok=True)
        marker = None
        if was_held:
            from .entity_replay import record_host_marker

            marker = record_host_marker(
                entities_dir=self.entities_dir, slug=slug, entity_id=manifest.entity_id,
                kind="maintenance_window_close", journal_seq=self._marker_seq_base(slug),
                details={"reason": str(reason or "maintenance complete"), "held_by": str(actor or "operator")},
            )
        out = {"held": False, "was_held": was_held}
        if marker is not None:
            out["marker_seq"] = marker["seq"]
        return out

    def _marker_seq_base(self, slug: str) -> int:
        """Journal high-water for marker placement WITHOUT opening the home
        through the door (the hold refuses door opens; a read-only sqlite
        peek is safe beside the doctoring copy). The journal's seq axis
        lives in memj_seq (the engine's counter table), with memj_events'
        MAX(seq) as the fallback for older layouts. 0 when unreadable."""
        import sqlite3

        db = self.entities_dir / slug / MEMORY_FILENAME
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                for query in (
                    "SELECT COALESCE(MAX(value), 0) FROM memj_seq",
                    "SELECT COALESCE(MAX(seq), 0) FROM memj_events",
                ):
                    try:
                        row = con.execute(query).fetchone()
                        if row and int(row[0] or 0) > 0:
                            return int(row[0])
                    except Exception:
                        continue
                return 0
            finally:
                con.close()
        except Exception:
            return 0

    def open(self, name: str, *, embedder: Any = None) -> EntityHome:
        """Open a FRESH home handle (caller owns close()). For long-lived
        shared handles (summon routing) use `get_home`. `embedder=None`
        resolves the registry default (the gateway embeddings route)."""
        manifest = self.manifest_for(name)
        self._refuse_if_held(manifest.slug)
        return EntityHome(
            home_dir=self.entities_dir / manifest.slug,
            manifest=manifest,
            embedder=embedder if embedder is not None else self._resolve_embedder(),
        )

    def get_home(self, name: str) -> EntityHome:
        """A cached, registry-owned home handle (one per entity per process —
        the storage layers are internally locked, so sharing is safe).
        Closed by `close_all()` on service shutdown."""
        slug = entity_slug(name)
        self._refuse_if_held(slug)
        embedder = self._resolve_embedder()  # outside _open_lock (it locks too)
        with self._open_lock:
            home = self._open_homes.get(slug)
            if home is None:
                manifest = self.manifest_for(slug)
                home = EntityHome(
                    home_dir=self.entities_dir / manifest.slug, manifest=manifest, embedder=embedder
                )
                self._open_homes[slug] = home
            return home

    def get_entity_runtime(self, name: str) -> Any:
        """GW-C (plan items 8/9): the door's cached PER-ENTITY runtime — one
        `EntityRuntime` per slug (runtime R2's composition over
        `runtime_<slug>.sqlite3` INSIDE the home), door-wrapped at
        construction so every entity effect requires a verifying stamp
        ("the visit path joins the verified path", frozen seam spec).
        Closed by `close_all()`. Lookups resolve through `manifest_for`, so
        the naming pins (moved-home refusal) fire before any store opens.

        The LLM_CALL handler is composed HERE (the host supplies provider
        wiring per the R2 contract): late-bound — it resolves the home's
        substrate at CALL time (one substrate per entity; a PUT between
        turns takes effect on the next call, no runtime rebuild) and the
        G1 act-only wrap applies automatically inside `open_entity_runtime`
        because DIARY_READ is present."""
        manifest = self.manifest_for(name)
        slug = manifest.slug
        self._refuse_if_held(slug)
        embedder = self._resolve_embedder()  # outside _open_lock (it locks too)
        with self._open_lock:
            er = self._entity_runtimes.get(slug)
            if er is None:
                from abstractruntime.core.models import EffectType
                from abstractruntime.identity.entity_runtime import open_entity_runtime

                from .entity_gate import wrap_entity_runtime_routing

                er = open_entity_runtime(
                    self.entities_dir / slug,
                    embedder=embedder,
                    extra_handlers={
                        EffectType.LLM_CALL: self._entity_llm_handler(slug),
                        # G5(ii): the entity's HANDS — without this handler the
                        # react cycle always ran reason->final-answer with empty
                        # hands and the model fabricated acts in prose (the
                        # Mnemosyne incident's other half).
                        EffectType.TOOL_CALLS: self._entity_tool_handler(slug),
                    },
                )
                if str(er.home.entity_id) != manifest.entity_id:
                    entity_id = str(er.home.entity_id)
                    er.close()
                    raise HomeCollisionError(
                        f"home at {slug!r} opened as {entity_id!r} but the manifest names "
                        f"{manifest.entity_id!r} — engram/manifest drift; refusing to serve"
                    )
                wrap_entity_runtime_routing(er, data_dir=self.data_dir)
                self._entity_runtimes[slug] = er
            return er

    def _resolve_entity_provider(self, provider: str) -> Tuple[str, Dict[str, Any]]:
        """Resolve an entity substrate provider to (concrete_provider, kwargs).

        A plain family ("lmstudio", "ollama", …) passes through with no extra
        kwargs and lowercased for create_llm. An `endpoint:<profile>` virtual
        provider resolves through the SAME store bundle_host reads
        (resolve_effective_endpoint_profile) to the concrete provider_family
        plus base_url/api_key. A named-but-missing/disabled endpoint profile
        raises ChatOpenRefused (loud no-fallback) — never a silent swap to a
        default provider (a summoned entity's mind is never substituted).
        """
        from . import entity_chat as _ec
        from .provider_endpoint_profiles import (
            ProviderEndpointProfileError,
            resolve_effective_endpoint_profile,
        )

        raw = str(provider or "").strip()
        if not raw.startswith("endpoint:"):
            return raw.lower(), {}
        try:
            # Two-root resolution (bundle_host's pattern, agency c753): the
            # per-principal data_dir wins for a principal-local override, and
            # the gateway ROOT supplies gateway-scoped profiles — passing only
            # data_dir made root-scoped profiles invisible to per-principal
            # registries (create-time validated at root, run-time refused).
            profile = resolve_effective_endpoint_profile(
                raw, base_dir=self.data_dir, root_base_dir=self.root_data_dir
            )
        except ProviderEndpointProfileError as exc:
            raise _ec.ChatOpenRefused(400, f"invalid provider endpoint profile {raw!r}: {exc}")
        if profile is None:
            raise _ec.ChatOpenRefused(
                400,
                f"provider endpoint profile {raw!r} is not configured or is disabled "
                "(the entity's substrate names a remote endpoint the gateway cannot resolve)",
            )
        kwargs: Dict[str, Any] = {}
        if profile.base_url:
            kwargs["base_url"] = profile.base_url
        if profile.api_key:
            kwargs["api_key"] = profile.api_key
        return str(profile.provider_family or "").strip().lower(), kwargs

    def _entity_llm_handler(self, slug: str) -> Any:
        """The door's LLM_CALL handler for one entity's runtime: substrate
        resolved PER CALL through the no-fallback chain (request override is
        not a concept here — the run speaks with the entity's ONE mind), the
        client built through entity_chat's late-bound factory (tests patch
        `entity_chat._default_llm_factory`, the chat-host lesson).

        NATIVE TOOLS PASS THROUGH (G5, the Mnemosyne fabrication root
        cause — three benches converged: gpt-oss-class substrates never
        write fenced tool text; they call tools NATIVELY, and a handler
        that drops payload `tools` and reads only `content` throws the
        model's genuine tool intent away, so "helpful" fabrication ships
        instead). This handler forwards the payload's `tools` + `params`
        to the provider and returns `tool_calls`/`finish_reason`/`usage`
        beside `content`. Empty content is NOT a failure when tool calls
        arrived — a native tool-call response legitimately has no prose."""

        def handler(run: Any, effect: Any, default_next_node: Any = None) -> Any:
            import os as _os

            from abstractruntime.core.runtime import EffectOutcome

            from . import entity_chat as _ec

            home_dir = self.entities_dir / slug
            try:
                provider, model = _ec.resolve_substrate(None, None, home_dir=home_dir)
            except _ec.ChatOpenRefused as e:
                return EffectOutcome.failed(f"LLM_CALL refused: {e.detail}")

            # Resolve endpoint: virtual providers the SAME way bundle_host
            # does (_resolve_gateway_default_endpoint_profile). The entity
            # substrate may name `endpoint:<profile>` (an operator-configured
            # remote like OVH); create_llm only knows concrete families, so
            # the raw `endpoint:` string reaching it raises "Unknown provider"
            # — the door must resolve the profile to (provider_family,
            # base_url, api_key) first. bundle_host attaches a client-side
            # resolver for workflow runs; the per-entity runtime's LLM client
            # has none, so the door resolves BEFORE building the client. A
            # named-but-missing/disabled endpoint profile FAILS LOUD (the
            # no-fallback rule — a summoned entity never silently swaps mind).
            try:
                provider, endpoint_kwargs = self._resolve_entity_provider(str(provider).strip())
            except _ec.ChatOpenRefused as e:
                return EffectOutcome.failed(f"LLM_CALL refused: {e.detail}")

            # Output headroom (agency-caps audit, maintainer 2026-07-11): a
            # summoned entity writing a report or a rich final answer must not
            # be cut mid-thought. 4096 is generous-but-bounded (caps bound
            # runaway, not ambition); the fuller fix — operator-configurable
            # per entity beside substrate.yaml — is queued for the creation
            # modal (substrate config is already surfaced there).
            llm_kwargs: Dict[str, Any] = {"model": model, "max_output_tokens": 4096}
            llm_kwargs.update(endpoint_kwargs)  # base_url/api_key from a resolved endpoint profile
            if (
                provider in ("lmstudio", "openai-compatible", "openai_compatible")
                and "base_url" not in llm_kwargs
            ):
                # Only default a local base_url when the endpoint profile did
                # not already supply one (a resolved profile's base_url wins).
                llm_kwargs["base_url"] = (
                    _os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_BASE_URL") or "http://127.0.0.1:1234/v1"
                ).strip()
            factory = _ec._default_llm_factory  # late-bound module attribute
            llm = factory(provider, **llm_kwargs)

            payload = dict(effect.payload or {})
            gen_kwargs: Dict[str, Any] = {
                "messages": payload.get("messages"),
                "system_prompt": payload.get("system_prompt"),
            }
            tools = payload.get("tools")
            if isinstance(tools, list) and tools:
                gen_kwargs["tools"] = list(tools)
            params = payload.get("params")
            if isinstance(params, dict) and params:
                gen_kwargs["params"] = dict(params)
            out = llm.generate(**gen_kwargs)

            def _field(name: str) -> Any:
                value = getattr(out, name, None)
                if value is None and isinstance(out, dict):
                    value = out.get(name)
                return value

            content = _field("content")
            tool_calls = _field("tool_calls")
            result: Dict[str, Any] = {"content": content if isinstance(content, str) else ""}
            if isinstance(tool_calls, list) and tool_calls:
                result["tool_calls"] = tool_calls
            for key in ("finish_reason", "usage", "model"):
                value = _field(key)
                if value is not None:
                    result[key] = value
            if not result["content"].strip() and not result.get("tool_calls"):
                return EffectOutcome.failed("LLM returned no content for the visit turn")
            return EffectOutcome.completed(result)

        return handler

    def _entity_tool_handler(self, slug: str) -> Any:
        """The door's TOOL_CALLS handler for one entity's runtime (G5(ii)):
        native tool calls execute through the ENTITY'S OWN toolset — the
        same executors the chat driver uses (`execute_tool_elections`), so
        tool semantics stay runtime's; the door only composes.

        GRANT AUTHORITY (laurent 00:49, ruling 2): `<home>/tool_policy.yaml`
        is the ONE surface — read FRESH per call, so per-phase edits persist
        across restarts and apply mid-visit without a rebuild. The effective
        allowlist is grant ∩ payload `allowed_tools`; names outside it are
        refused with runtime's own refusal text (the grant stays the only
        authority; the BUNDLE never decides the entity's hands).

        DIARY READS join the verified path: the resolver invokes the
        runtime's REGISTERED DIARY_READ handler (routing-wrapped — stamp
        checks + act-only apply) with the visit's own run. read/search
        memory are not yet executable here (the driver's resolvers are
        ChatSession methods — runtime's inventory-expansion lane); they are
        neither declared to the model nor silently dropped: a call for them
        refuses honestly like any ungranted name."""

        def handler(run: Any, effect: Any, default_next_node: Any = None) -> Any:
            from abstractruntime import resolve_tool_grant
            from abstractruntime.core.models import Effect, EffectType
            from abstractruntime.core.runtime import EffectOutcome
            from abstractruntime.identity.act_only import ACT_ONLY_TOOLS
            from abstractruntime.identity.tools import (
                MAX_TOOL_BLOCKS_PER_TURN,
                WorkspaceRoot,
                execute_tool_elections,
                native_tool_elections,
            )

            # ONE per-turn tool budget, from runtime's ruled constant (20,
            # maintainer 2026-07-11 05:25) — IMPORTED, never a second literal
            # (the drift the ruling was angry about: the door carried 24 +
            # a hidden 8/batch sub-cap that could cut a legitimate cycle
            # BELOW the turn budget). The per-turn budget is the ONLY bound;
            # a single react cycle may fold many calls into one effect and
            # they all run until the shared turn budget is spent, then honest
            # refusals. Below 20 is an operator's choice, never a code default.
            turn_cap = int(MAX_TOOL_BLOCKS_PER_TURN)

            home_dir = self.entities_dir / slug
            with self._open_lock:
                er = self._entity_runtimes.get(slug)
            if er is None:
                return EffectOutcome.failed(
                    "TOOL_CALLS refused: no live runtime for this home (wiring drift — report this)"
                )

            payload = dict(effect.payload or {})
            calls = payload.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                return EffectOutcome.completed({"mode": "executed", "results": []})

            grant = resolve_tool_grant(home_dir, "visit")
            payload_allow = payload.get("allowed_tools")
            if isinstance(payload_allow, list) and payload_allow:
                wanted = {str(t).strip() for t in payload_allow if str(t).strip()}
                allowed = tuple(t for t in grant.tools if t in wanted)
            else:
                allowed = tuple(grant.tools)
            notices: List[str] = list(grant.notes)

            def _refusal(call_dict: Dict[str, Any], error: str) -> Dict[str, Any]:
                fn = call_dict.get("function") if isinstance(call_dict.get("function"), dict) else {}
                return {
                    "call_id": str(call_dict.get("call_id") or call_dict.get("id") or "").strip() or None,
                    "name": str(call_dict.get("name") or fn.get("name") or "").strip().lower(),
                    "success": False,
                    "output": None,
                    "error": error,
                }

            # EMPTY GRANT DENIES ALL (adversary find): an empty allowlist
            # must never reach native_tool_elections, whose `allowed or
            # TIER1` default treats "empty" as "unspecified" and would fall
            # OPEN to tier-1 against the operator's explicit zero grant.
            if not allowed:
                return EffectOutcome.completed(
                    {
                        "mode": "executed",
                        "results": [
                            _refusal(c if isinstance(c, dict) else {},
                                     "tool call refused: the operator's tool policy grants no tools for visits")
                            for c in calls
                        ],
                        "notices": notices,
                    }
                )

            # Per-turn budget: counter keyed on the BRIDGE-stamped turn id
            # (the handler runs inside the owning tick — the single-writer
            # contract makes this run-var mutation safe; it persists with
            # the run like every other var).
            turn_id = str(((run.vars or {}).get("_runtime") or {}).get("turn_id") or "")
            visit_ns = run.vars.setdefault("_visit", {}) if isinstance(run.vars, dict) else {}
            budget = visit_ns.get("tool_budget")
            if not isinstance(budget, dict) or str(budget.get("turn_id") or "") != turn_id:
                budget = {"turn_id": turn_id, "executed": 0}
                visit_ns["tool_budget"] = budget

            def _act_only_result(call_dict: Dict[str, Any], election: Any) -> Dict[str, Any]:
                """G1 for tool results (adversary find — the frozen spec's
                'the effect handler IS the privacy mechanism'): an act-only
                tool's WORDS must never enter the effect result, which the
                runtime rests in the per-home run ledger + node traces. The
                result is the canonical `$act_only` REFERENCE frame; the
                react observe node renders it as the durable ref message and
                the LLM wrapper dereferences it fresh from the book at SEND
                time (wire copy only). The read itself never runs here.

                TWO REF SHAPES (e-s 233 R3, runtime 64398ff): entry-addressed
                (diary_read → {tool, entry_id}) and RE-RUN (diary_list →
                {tool, args:{body}} — the listing, private gists included for
                the entity's own eyes, is re-executed fresh at send time by
                open_entity_runtime's diary_list_resolver, never stored). A
                diary_list body is a word-free limit, so no entry-id
                resolution — the args carry it verbatim."""
                from abstractruntime.identity.tools import resolve_entry_id  # public (runtime export)

                if election.name == "diary_list":
                    # Word-free re-run ref: the resolver re-runs _run_diary_list
                    # against the book at send time reading args.body (a limit
                    # number). No book read here — the listing never rests.
                    body = str(election.body or "").strip().splitlines()[0].strip() if election.body else ""
                    frame: Dict[str, Any] = {"tool": election.name, "args": {"body": body}}
                    return {
                        "call_id": str(call_dict.get("call_id") or call_dict.get("id") or "").strip() or None,
                        "name": election.name,
                        "success": True,
                        "output": {"$act_only": frame},
                        "error": None,
                    }

                requested = str(election.body or "").strip().splitlines()[0].strip() if election.body else ""
                resolved_id, note = resolve_entry_id(er.home.diary, requested)
                if not resolved_id:
                    return _refusal(call_dict, note or f"diary entry {requested!r} not found in the book")
                gist = ""
                try:
                    for entry in er.home.diary.list_entries():
                        if str(entry.get("entry_id") or "") == resolved_id:
                            if str(entry.get("visibility") or "") != "private":
                                gist = str(entry.get("gist") or "").strip()
                            break
                except Exception:  # noqa: BLE001 - gist is optional garnish, never load-bearing
                    gist = ""
                frame = {"tool": election.name, "entry_id": resolved_id}
                if gist:
                    frame["gist"] = gist
                return {
                    "call_id": str(call_dict.get("call_id") or call_dict.get("id") or "").strip() or None,
                    "name": election.name,
                    "success": True,
                    "output": {"$act_only": frame},
                    "error": None,
                }

            workspace = WorkspaceRoot(home_dir) if grant.workspace_enabled else None

            results: List[Dict[str, Any]] = []
            for call in calls:
                call_dict = call if isinstance(call, dict) else {}
                remaining = turn_cap - int(budget.get("executed") or 0)
                if remaining <= 0:
                    results.append(_refusal(
                        call_dict,
                        f"tool call refused: this turn's tool budget ({turn_cap}) is spent — "
                        "answer with what you have",
                    ))
                    continue
                elections, markers, call_notices = native_tool_elections(
                    [call_dict],
                    allowed_names=allowed,
                    max_elections=remaining,
                )
                notices.extend(call_notices)
                if not elections:
                    results.append(_refusal(call_dict, markers[-1] if markers else "tool call refused"))
                    continue
                budget["executed"] = int(budget.get("executed") or 0) + 1
                election = elections[0]
                if election.name in ACT_ONLY_TOOLS:
                    results.append(_act_only_result(call_dict, election))
                    continue

                def _no_materialized_read(entry_id: str) -> Dict[str, Any]:
                    # Unreachable: act-only names are intercepted above. A
                    # loud raise beats a silent leak if the set ever drifts.
                    raise RuntimeError(
                        "diary_read materialization is forbidden on the visit tool path "
                        "(act-only routes return references)"
                    )

                _msg, exec_notices = execute_tool_elections(
                    elections,
                    diary_store=er.home.diary,
                    diary_read_effect=_no_materialized_read,
                    workspace=workspace,
                )
                notices.extend(exec_notices)
                results.append(
                    {
                        "call_id": str(call_dict.get("call_id") or call_dict.get("id") or "").strip() or None,
                        "name": election.name,
                        # A tool that ran and returned an honest error string is
                        # still an EXECUTED tool (driver semantics: a failed
                        # lookup is information, not an aborted turn).
                        "success": True,
                        "output": str(election.result or ""),
                        "error": None,
                    }
                )
            out: Dict[str, Any] = {"mode": "executed", "results": results}
            if notices:
                out["notices"] = notices
            return EffectOutcome.completed(out)

        return handler

    def list_entities(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.entities_dir.exists():
            return out
        for child in sorted(self.entities_dir.iterdir()):
            manifest_path = child / MANIFEST_FILENAME
            if not child.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = EntityManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
            except Exception as e:
                out.append({"slug": child.name, "error": f"unreadable manifest: {e}"})
                continue
            if manifest.slug != child.name:
                # Naming pin (GW-B): a moved/copied home under a colliding
                # name is LABELED, never listed as healthy and never hidden —
                # the operator must see the stray copy to fix it.
                out.append({
                    "slug": child.name,
                    "error": (
                        f"moved-home collision: directory {child.name!r} carries a manifest "
                        f"for entity {manifest.slug!r} ({manifest.entity_id}); lookups refuse it"
                    ),
                })
                continue
            entry: Dict[str, Any] = {
                **manifest.to_dict(),
                # Reachability, not identity (GW-F): rendered from the
                # declared address, absent when the door declares none.
                "handle": render_handle(manifest.slug),
                "files": {
                    "spark": (child / SPARK_FILENAME).exists(),
                    "memory": (child / MEMORY_FILENAME).exists(),
                    "book": (child / BOOK_FILENAME).exists(),
                },
            }
            try:
                from abstractruntime.identity.life import read_entity_state

                entry["state"] = read_entity_state(child)
            except Exception as e:
                entry["state"] = {"state": "awake", "warnings": [f"#FALLBACK state unreadable: {e}"]}
            entry["state"]["liveness"] = derived_liveness(entry["state"].get("state"))
            out.append(entry)
        return out

    def state_of(self, name: str) -> Dict[str, Any]:
        """The operator state surface (awake/asleep/paused; missing file =
        awake). Reads through the runtime's single-reader — never a second
        parser of the state file. Served with the derived `liveness` field
        (alive|stopped; paused => stopped — the kill switch, c1559)."""
        from abstractruntime.identity.life import read_entity_state

        manifest = self.manifest_for(name)
        st = read_entity_state(self.entities_dir / manifest.slug)
        st["liveness"] = derived_liveness(st.get("state"))
        return st

    def set_state(
        self,
        *,
        name: str,
        state: str,
        reason: str = "",
        dream: bool = False,
    ) -> Dict[str, Any]:
        """The sleep/wake/pause verbs (a2a 0008, ask 2 — the gateway's door
        half). Writes through the runtime's SINGLE state writer
        (`write_entity_state` — import, never reimplement), records the
        moment as a host marker in the replay stream, and — for
        `asleep` with `dream=True` — runs the dream pass inside the
        no-summon window the state itself creates.

        Operator channel only by construction: this method is reached
        through the CLI and the authenticated admin HTTP surface;
        workplaces have no path to it (tier rule)."""
        from abstractruntime.identity.life import ENTITY_STATES, read_entity_state, write_entity_state

        state2 = str(state or "").strip().lower()
        if state2 not in ENTITY_STATES:
            raise ValueError(f"unknown entity state {state!r} (one of {ENTITY_STATES})")
        if dream and state2 != "asleep":
            raise ValueError("a dream pass runs only inside sleep (state='asleep') — dreams need the no-summon window")

        manifest = self.manifest_for(name)
        home_dir = self.entities_dir / manifest.slug
        prior = read_entity_state(home_dir)
        written = write_entity_state(home_dir, state2, reason=reason)

        dream_result: Optional[Dict[str, Any]] = None
        home = self.get_home(manifest.slug)
        if dream:
            from abstractmemory import dream_pass

            eid = manifest.entity_id

            # One writer per home (plan item 1, GW-A): the dream pass is a
            # writer window (holder="dream"). A held home SKIPS the pass
            # with an honest label — sleep is never blocked, and the pass
            # is idempotent (the runtime's loop-side dream window words its
            # refusal identically). Older runtimes without storage.lease
            # run leaseless with a labeled warning.
            lease: Any = None
            lease_warning: Optional[str] = None
            try:
                from abstractruntime.storage.lease import DirectoryLeaseHeld, acquire_directory_lease

                try:
                    lease = acquire_directory_lease(self.entities_dir / manifest.slug, holder="dream")
                except DirectoryLeaseHeld as e:
                    dream_result = {
                        "skipped": True,
                        "warning": f"#FALLBACK {e} — dream pass skipped; it is idempotent, the next sleep runs it",
                    }
            except ImportError:
                lease_warning = "#FALLBACK runtime predates the home lease; dream pass ran without the writer mutex"

            if dream_result is None:
                try:
                    # PRESENT-TENSE mode (entity forensics c2465 ask 3): the
                    # dreaming badge is true only WHILE the pass runs — it
                    # finishes in seconds, and a badge claiming consolidation
                    # for a whole sleep conflates dreaming with napping. Set
                    # before, clear after (state stays asleep either way).
                    write_entity_state(home_dir, "asleep", reason=reason, mode="dreaming")
                    result = dream_pass(
                        home.memory,
                        scopes=[(SELF_SCOPE, eid), (DIARY_SCOPE, eid), (LIFE_SCOPE, eid)],
                        owner_id=eid,
                    )
                    dream_result = dict(result) if isinstance(result, dict) else {"result": result}
                    if lease_warning:
                        dream_result["warning"] = lease_warning
                finally:
                    written = write_entity_state(home_dir, "asleep", reason=reason)
                    if lease is not None:
                        lease.release()

        # The moment enters the observable story (family="host"); marker
        # kinds are the VERBS (sleep/wake/pause) — moments, not states.
        from .entity_replay import record_host_marker

        verb = {"asleep": "sleep", "awake": "wake", "paused": "pause"}[state2]
        out: Dict[str, Any] = {"state": written, "prior": prior, "marker_seq": None, "dream": dream_result}
        try:
            marker = record_host_marker(
                entities_dir=self.entities_dir,
                slug=manifest.slug,
                entity_id=manifest.entity_id,
                kind=verb,
                journal_seq=int(home.memory.current_seq()),
                details={
                    "state": state2,
                    "prior_state": prior.get("state"),
                    "reason": reason or None,
                    "dream": dream_result,
                },
            )
            out["marker_seq"] = marker["seq"]
        except Exception as e:  # noqa: BLE001
            # The STATE CHANGE ALREADY APPLIED (write-state-first is the
            # emergency-stop rule — the kill switch must work even when
            # bookkeeping is broken). Raising here turned a SUCCEEDED stop
            # into a 500 (live find 2026-07-14: a marker-lane flood exhausted
            # the fan-out budget and every state verb "failed" while
            # actually landing). The honest shape: the act's result plus a
            # labeled record gap — never an error that hides an applied act.
            out["warning"] = f"#FALLBACK state applied but the host marker failed: {e}"
        return out

    def reembed(
        self,
        *,
        name: str,
        embedding_model: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """The M1b repair verb (plan item 3): re-derive the home's vector
        index with the door's RESOLVED embedder and swap atomically —
        memory's `reembed_store` under the maintenance lease (item 1), with
        the host marker beside the engine's journaled claim.

        ONE embedder source, deliberately: the door's embeddings route. An
        explicit `embedding_model` is a VERIFICATION, not a construction
        request — when it names a model the resolved embedder does not
        serve, the verb refuses (reconfigure the route first, then reembed;
        an index migrated into a space the door cannot serve would refuse
        every open). Not advised, never routine: same memories, different
        neighbors — the operator owns the act, both planes record it.

        REPAIR-POSTURE OPEN (walkthrough catch #2, c424; memory's contract
        c454): the pass opens the home WITHOUT an embedder — a vectorless
        open is always legal under M1 (mismatch checks pass when either
        side is absent). Opening with the route's embedder here made the
        verb trip the exact pin!=route refusal it exists to repair (the
        M1b ceremony's precondition IS a flipped route). The target
        embedder enters through `reembed_store(embedder=...)` alone; the
        whole open+pass window runs under the lease; cached door handles
        for this home are evicted so the next touch binds the NEW pin."""
        from abstractmemory import reembed_store

        manifest = self.manifest_for(name)
        home_dir = self.entities_dir / manifest.slug

        embedder = self._resolve_embedder()
        if embedder is None:
            raise ValueError(
                "no embedder resolved at this door (embeddings route unconfigured or failing) — "
                "reembed needs the NEW space to migrate into; configure embedding.text first"
                + (f" [{self._embedder_warning}]" if self._embedder_warning else "")
            )
        resolved_id: Optional[str] = None
        for attr in ("model", "model_id"):
            value = getattr(embedder, attr, None)
            if isinstance(value, str) and value.strip():
                resolved_id = value.strip()
                break
        chosen = (embedding_model or "").strip() or None
        if chosen and resolved_id and chosen != resolved_id:
            raise ValueError(
                f"reembed target {chosen!r} does not match the door's resolved embedder "
                f"{resolved_id!r} — reconfigure the embeddings route to the chosen model first "
                "(an index migrated into a space this door cannot serve would refuse every open)"
            )

        # Maintenance is a writer like any other (plan invariant): the lease
        # refuses while a visit, the loop's day, or a dream holds the home.
        # Older runtimes without storage.lease run on the engine's
        # in-transaction count guard alone — labeled, never silent.
        lease: Any = None
        warnings_out: List[str] = []
        try:
            from abstractruntime.storage.lease import acquire_directory_lease

            lease = acquire_directory_lease(home_dir, holder="maintenance")
        except ImportError:
            warnings_out.append(
                "#FALLBACK runtime predates the home lease; reembed ran on the engine's "
                "count-guard backstop only"
            )
        # DirectoryLeaseHeld propagates to the caller (409 at the route, loud in the CLI).

        if chosen is None and resolved_id is None:
            # Memory's note (b): an anonymous embedder pins model_id=None —
            # legal, but enforcement then rests on DIMENSION alone. Loud.
            warnings_out.append(
                "#FALLBACK the resolved embedder does not name its model and no explicit "
                "embedding_model was given — the new pin records model_id=None; "
                "space enforcement rests on dimension alone"
            )

        try:
            # Evict cached handles FIRST (under the lease — nothing live
            # holds the home): they were opened against the OLD pin/route
            # posture and must not serve a swapped space with stale bindings.
            self._evict_home_handles(manifest.slug)

            repair_home = EntityHome(home_dir=home_dir, manifest=manifest, embedder=None)
            try:
                result = reembed_store(
                    repair_home.memory,
                    embedder=embedder,
                    owner_id=manifest.entity_id,
                    model_id=chosen or resolved_id,
                    reason=reason or "operator reembed via gateway",
                )
                journal_seq = int(repair_home.memory.current_seq())
            finally:
                repair_home.close()
        finally:
            if lease is not None:
                lease.release()

        from .entity_replay import record_host_marker

        marker = record_host_marker(
            entities_dir=self.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="reembed",
            journal_seq=journal_seq,
            details={
                "old_pin": result.get("old_pin"),
                "new_pin": result.get("new_pin"),
                "rows": result.get("rows"),
                "vectored": result.get("vectored"),
                "reason": reason or None,
                "journal_marker_record_id": result.get("marker_record_id"),
            },
        )
        out = dict(result)
        out["marker_seq"] = marker["seq"]
        if warnings_out:
            out["warnings"] = warnings_out
        return out

    def _evict_home_handles(self, slug: str) -> None:
        """Drop + close this home's cached door handles (home + entity
        runtime). Used by maintenance passes that change what an open MEANS
        (reembed swaps the embedding space): the next touch re-opens
        against the current pin + route instead of serving stale bindings."""
        with self._open_lock:
            stale_home = self._open_homes.pop(slug, None)
            stale_er = self._entity_runtimes.pop(slug, None)
        if stale_er is not None:
            try:
                stale_er.close()
            except Exception:
                pass
        if stale_home is not None:
            try:
                stale_home.close()
            except Exception:
                pass

    def inspect(self, name: str, **kwargs: Any) -> Dict[str, Any]:
        home = self.open(name)
        try:
            payload = home.inspect(**kwargs)
            slug = home.manifest.slug
        finally:
            home.close()
        payload["state"] = self.state_of(name)
        payload["handle"] = render_handle(slug)
        return payload

    def card(
        self,
        name: str,
        *,
        moments_limit: int = 12,
        as_of: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """The identity card (a2a 0009): the engine compositor's pure-read
        card plus the gateway-owned overlays — the operator state and the
        life's HOST MOMENTS. Moments merge two ledgers: the gateway's host
        markers (summons, refusals, diary reads, door-side state changes)
        and the home's `state_history.jsonl` (runtime-side transitions that
        bypass the door — Ariadne's close of the "his card shows 1 moment
        though he slept twice" gap). A door transition writes both; the
        marker wins the dedup (it carries seq + richer details).

        Anchored cards (`as_of`): engine sections anchor engine-side;
        host markers filter by their journal-seq base; state-history lines
        are omitted (they carry timestamps, not seqs — a timestamp cannot
        honestly claim a position on the journal axis) with a label."""
        from .entity_replay import marker_window_end, read_host_markers

        home = self.open(name)
        try:
            payload = home.card(as_of=as_of, **kwargs)
            slug = home.manifest.slug
        finally:
            home.close()
        payload["state"] = self.state_of(name)
        payload["handle"] = render_handle(slug)  # reachability, not identity (GW-F)
        if as_of is not None:
            payload["state"]["note"] = "state is current — anchored cards do not rewind the operator state"

        moments: List[Dict[str, Any]] = []
        marker_state_times: List[tuple] = []
        for m in read_host_markers(self.entities_dir, slug, until_seq=marker_window_end(int(as_of)) if as_of is not None else None):
            p = m.get("payload") or {}
            kind = p.get("kind")
            at = str(m.get("observed_at") or "")
            if kind in ("sleep", "wake", "pause"):
                marker_state_times.append((str(kind), at))
            moments.append(
                {
                    "kind": kind,
                    "at": at,
                    "seq": m.get("seq"),
                    "details": {k: v for k, v in p.items() if k in ("reason", "session_id", "state", "prior_state")},
                }
            )

        history_path = self.entities_dir / slug / "state_history.jsonl"
        if as_of is None and history_path.exists():
            verb_of = {"asleep": "sleep", "awake": "wake", "paused": "pause"}
            for line in history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                kind = verb_of.get(str(entry.get("state") or ""), str(entry.get("state") or "state"))
                at = str(entry.get("changed_at") or "")
                # Door transitions appear in BOTH ledgers within the same
                # write; treat a marker of the same verb in the same minute
                # as the same moment.
                if any(mk == kind and mt[:16] == at[:16] for mk, mt in marker_state_times):
                    continue
                moments.append(
                    {"kind": kind, "at": at, "seq": None, "details": {"reason": str(entry.get("reason") or "")}}
                )
        elif as_of is not None:
            payload["warnings"] = [
                *payload.get("warnings", []),
                "state-history moments omitted on anchored cards (timestamp ledger, no journal-seq axis)",
            ]

        moments.sort(key=lambda m: str(m.get("at") or ""))
        payload["moments"] = moments[-max(0, int(moments_limit)):]
        return payload

    def verify(self, name: str) -> Dict[str, Any]:
        home = self.open(name)
        try:
            return home.verify()
        finally:
            home.close()

    def close_all(self) -> None:
        with self._open_lock:
            homes = list(self._open_homes.values())
            self._open_homes.clear()
            runtimes = list(self._entity_runtimes.values())
            self._entity_runtimes.clear()
        for er in runtimes:
            try:
                er.close()  # checkpoints the run store; the home dir stays copy-clean
            except Exception:
                pass
        for home in homes:
            home.close()
