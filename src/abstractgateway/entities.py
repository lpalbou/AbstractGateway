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
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "EntityHome",
    "EntityManifest",
    "EntityRegistry",
    "entity_slug",
]

ENTITY_FORMAT_VERSION = 1
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

SPARK_FILENAME = "spark.yaml"
MEMORY_FILENAME = "memory.sqlite3"
BOOK_FILENAME = "home.sqlite3"
MANIFEST_FILENAME = "manifest.json"

# The identity scopes inside one home file (keystone conventions):
# self = engram records + valence events + self bindings; diary = act-memory
# projections (hardcoded by DIARY_WRITE); life = work-session experience.
SELF_SCOPE = "self"
DIARY_SCOPE = "diary"
LIFE_SCOPE = "life"


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
    return slug


def _utc_now_iso() -> str:
    from abstractruntime.core.runtime import utc_now_iso

    return utc_now_iso()


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

    def __init__(self, *, home_dir: Path, manifest: EntityManifest, embedder: Any = None) -> None:
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
        try:
            self.store = SQLiteTripleStore(memory_path, embedder=embedder)
        except TypeError:
            self.store = SQLiteTripleStore(memory_path)
        self.journal = SQLiteJournal(memory_path)
        self.memory = MemorySystem(store=self.store, journal=self.journal, embedder=embedder)
        self._book_ledger = SqliteLedgerStore(SqliteDatabase(str(self.home_dir / BOOK_FILENAME)))
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

        manifest_ok = (
            self.manifest.entity_id == f"entity:{self.manifest.slug}@{self.manifest.home_id}"
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
        for obj in (self.store, self.journal, self.memory):
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "created": self.created,
            "manifest": dict(self.manifest),
            "warnings": list(self.warnings),
        }


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

    def __init__(self, *, data_dir: Path, embedder_factory: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.entities_dir = self.data_dir / "entities"
        self._create_lock = threading.Lock()
        self._open_homes: Dict[str, EntityHome] = {}
        self._open_lock = threading.Lock()
        self._embedder_factory = embedder_factory
        self._embedder: Any = None
        self._embedder_resolved = False
        self._embedder_lock = threading.Lock()
        self._embedder_warning: Optional[str] = None

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

    def create(
        self,
        *,
        name: str,
        spark: Optional[Mapping[str, Any]] = None,
        spark_text: Optional[str] = None,
        framework: bool = True,
    ) -> EntityCreateResult:
        """Create (or idempotently re-adopt) an entity home.

        Order: lint -> store the spark verbatim -> engram -> manifest. The
        engram's own guards do the heavy lifting and their errors are
        surfaced verbatim (they are written for humans): re-running the same
        spark is a no-op (`created=False`); a CHANGED document under the
        same version is refused — the spark is for life.
        """
        import yaml
        from abstractmemory import DEFAULT_SPARK_TEMPLATE, canonical_spark_hash, engram, lint_spark

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

        with self._create_lock:
            home_dir = self.entities_dir / slug
            spark_path = home_dir / SPARK_FILENAME
            manifest_path = home_dir / MANIFEST_FILENAME
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
                # entity_id (slug@home_id), so a crash between manifest and
                # engram retries into the SAME identity instead of minting a
                # second home_id over a half-planted core.
                home_id = f"home-{secrets.token_hex(4)}"
                manifest = EntityManifest(
                    entity_id=f"entity:{slug}@{home_id}",
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
            home = EntityHome(home_dir=home_dir, manifest=manifest, embedder=self._resolve_embedder())
            try:
                result = engram(
                    home.memory,
                    spark_doc,
                    owner_id=manifest.entity_id,
                    spark_artifact_ref=SPARK_FILENAME,
                )
            finally:
                home.close()

        return EntityCreateResult(
            entity_id=manifest.entity_id,
            created=bool(result.created),
            manifest=manifest.to_dict(),
            warnings=warnings + list(result.warnings),
        )

    # -- open / list / reads -----------------------------------------------

    def manifest_for(self, name: str) -> EntityManifest:
        slug = entity_slug(name)
        manifest_path = self.entities_dir / slug / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise KeyError(f"entity {slug!r} not found under {self.entities_dir}")
        return EntityManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))

    def open(self, name: str, *, embedder: Any = None) -> EntityHome:
        """Open a FRESH home handle (caller owns close()). For long-lived
        shared handles (summon routing) use `get_home`. `embedder=None`
        resolves the registry default (the gateway embeddings route)."""
        manifest = self.manifest_for(name)
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
            entry: Dict[str, Any] = {
                **manifest.to_dict(),
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
            out.append(entry)
        return out

    def state_of(self, name: str) -> Dict[str, Any]:
        """The operator state surface (awake/asleep/paused; missing file =
        awake). Reads through the runtime's single-reader — never a second
        parser of the state file."""
        from abstractruntime.identity.life import read_entity_state

        manifest = self.manifest_for(name)
        return read_entity_state(self.entities_dir / manifest.slug)

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
            result = dream_pass(
                home.memory,
                scopes=[(SELF_SCOPE, eid), (DIARY_SCOPE, eid), (LIFE_SCOPE, eid)],
                owner_id=eid,
            )
            dream_result = dict(result) if isinstance(result, dict) else {"result": result}

        # The moment enters the observable story (family="host"); marker
        # kinds are the VERBS (sleep/wake/pause) — moments, not states.
        from .entity_replay import record_host_marker

        verb = {"asleep": "sleep", "awake": "wake", "paused": "pause"}[state2]
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
        return {"state": written, "prior": prior, "marker_seq": marker["seq"], "dream": dream_result}

    def inspect(self, name: str, **kwargs: Any) -> Dict[str, Any]:
        home = self.open(name)
        try:
            payload = home.inspect(**kwargs)
        finally:
            home.close()
        payload["state"] = self.state_of(name)
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
        from .entity_replay import read_host_markers

        home = self.open(name)
        try:
            payload = home.card(as_of=as_of, **kwargs)
            slug = home.manifest.slug
        finally:
            home.close()
        payload["state"] = self.state_of(name)
        if as_of is not None:
            payload["state"]["note"] = "state is current — anchored cards do not rewind the operator state"

        moments: List[Dict[str, Any]] = []
        marker_state_times: List[tuple] = []
        for m in read_host_markers(self.entities_dir, slug, until_seq=float(as_of) + 0.9995 if as_of is not None else None):
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
        for home in homes:
            home.close()
