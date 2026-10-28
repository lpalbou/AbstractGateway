from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


_TRUE = {"1", "true", "yes", "y", "on"}
_BACKEND_CLASS_NAMES = {
    "lancedb": "LanceDBTripleStore",
    "sqlite": "SQLiteTripleStore",
    "memory": "InMemoryTripleStore",
}


def _env_first(*keys: str) -> Optional[str]:
    for key in keys:
        raw = os.getenv(str(key))
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(str(name))
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in _TRUE


def _normalize_backend(raw: Optional[str]) -> str:
    backend = str(raw or "lancedb").strip().lower().replace("-", "_")
    if backend in {"memory", "in_memory", "inmemory"}:
        return "memory"
    if backend in {"sqlite", "sqlite3"}:
        return "sqlite"
    if backend in {"lance", "lancedb", "vector"}:
        return "lancedb"
    raise ValueError("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND must be one of: lancedb, sqlite, memory")


# Owner ids of the KG scopes, for a caller that holds a session id but no run.
#
# With a run, the gateway asks the runtime: `resolve_scope_owner_id(run, scope=...)`
# (abstractruntime.integrations.abstractmemory.effect_handlers). A thin client
# that only knows its session id (or asks for global memory) has no run to hand
# over, so the derivation is written here as the PUBLIC rule the runtime applies:
# `session_memory_<sid>` when the id is safe, else `session_memory_sha_<sha256[:32]>`;
# global = `$ABSTRACTRUNTIME_GLOBAL_MEMORY_RUN_ID` when safe, else `global_memory`.
# `tests/test_gateway_memory_owner_ids.py` pins parity with the runtime on real
# RunStates, so a runtime change turns that test red instead of drifting.
_SAFE_OWNER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def session_memory_owner_id(session_id: str) -> str:
    """Owner id of a session's KG memory (= the runtime's for any run of that session)."""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id must be a non-empty string")
    if _SAFE_OWNER_ID_RE.match(sid):
        oid = f"session_memory_{sid}"
        if _SAFE_OWNER_ID_RE.match(oid):
            return oid
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
    return f"session_memory_sha_{digest}"


def global_memory_owner_id() -> str:
    """Owner id of the plane's global KG memory (= the runtime's `scope="global"`)."""
    raw = str(os.environ.get("ABSTRACTRUNTIME_GLOBAL_MEMORY_RUN_ID") or "").strip()
    if raw and _SAFE_OWNER_ID_RE.match(raw):
        return raw
    return "global_memory"


@dataclass(frozen=True)
class GatewayMemoryStoreConfig:
    backend: str
    path: Optional[Path]
    require_vector: bool = False
    # The plane data folder the config was resolved for (the memory home lives under it).
    base_dir: Optional[Path] = None

    def public_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "path": str(self.path) if self.path is not None else None,
            "require_vector": bool(self.require_vector),
        }


@dataclass
class GatewayMemoryStoreResolution:
    store: Any
    config: GatewayMemoryStoreConfig
    capabilities: Dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    # `SQLiteJournal` on the store's own file (sqlite backend, `with_journal=True`): the caller closes it.
    journal: Any = None

    def public_dict(self) -> Dict[str, Any]:
        return {
            **self.config.public_dict(),
            "capabilities": dict(self.capabilities),
            "warnings": list(self.warnings),
        }


def resolve_memory_store_config(
    *,
    base_dir: Path,
    backend: Optional[str] = None,
    path: Optional[Path | str] = None,
    require_vector: Optional[bool] = None,
) -> GatewayMemoryStoreConfig:
    """Resolve Gateway-owned memory store settings without importing AbstractMemory."""

    resolved_backend = _normalize_backend(
        backend
        or _env_first(
            "ABSTRACTGATEWAY_MEMORY_STORE_BACKEND",
            "ABSTRACTGATEWAY_MEMORY_BACKEND",
        )
    )
    raw_path = path or _env_first("ABSTRACTGATEWAY_MEMORY_STORE_PATH", "ABSTRACTGATEWAY_MEMORY_PATH")
    base = Path(base_dir).expanduser().resolve()

    if raw_path:
        resolved_path = Path(raw_path).expanduser().resolve()
    elif resolved_backend == "lancedb":
        resolved_path = base / "abstractmemory" / "kg"
    elif resolved_backend == "sqlite":
        resolved_path = base / "abstractmemory" / "kg.sqlite3"
    else:
        resolved_path = None

    if require_vector is None:
        require_vector = _env_bool("ABSTRACTGATEWAY_MEMORY_REQUIRE_VECTOR", default=False)

    return GatewayMemoryStoreConfig(
        backend=resolved_backend,
        path=resolved_path,
        require_vector=bool(require_vector),
        base_dir=base,
    )


def resolve_memory_home(config: GatewayMemoryStoreConfig) -> Path:
    """The plane's memory HOME: the one SQLite file that holds its memory graph (triples, journal,
    revision log, provenance index; AbstractMemory ADR 0003) in this phase.

    - backend `sqlite`: the home IS the KG store file (`<plane>/abstractmemory/kg.sqlite3`, or the
      `ABSTRACTGATEWAY_MEMORY_STORE_PATH` override), so `/kg/query`, `/memory/*` and the runtime's
      memory effects read and write one file;
    - backend `lancedb` (the default) or `memory`: LanceDB cannot carry the graph (no revisions,
      `close`/`narrow` unsupported), so the home is `<plane>/abstractmemory/memory.sqlite3`, beside
      the LanceDB KG, created on first write through `MemoryFacade.open_mind`.

    One SQLite home per plane with LanceDB as an optional accelerator is AbstractMemory backlog 0057;
    moving the gateway default off LanceDB (and importing existing plane KG data into the home) waits
    for the operator's decision 2 of the memory transition. Until then the LanceDB KG and the home
    are two stores. Entity minds (`entities/<slug>/memory.sqlite3`) are their own homes, not this one.
    """
    if config.backend == "sqlite" and config.path is not None:
        return Path(config.path)
    if config.base_dir is None:
        raise ValueError("resolve_memory_home needs a config resolved for a plane (resolve_memory_store_config(base_dir=...))")
    return Path(config.base_dir) / "abstractmemory" / "memory.sqlite3"


def open_memory_home(config: GatewayMemoryStoreConfig, *, create: bool, **stores: Any) -> Optional[Any]:
    """The plane's `MemoryFacade` over its home (`resolve_memory_home`) and the runtime `stores` that
    hold its ledger sources (`ledger_store`, `run_store`, `artifact_store`, ...). With `create=False`
    a home that does not exist yet is not created: None (a read never makes files). The caller closes
    `facade.journal` and `facade.store`."""
    home = resolve_memory_home(config)
    if not home.exists():
        if not create:
            return None
        home.parent.mkdir(parents=True, exist_ok=True)
    return _memory_facade().MemoryFacade.open_mind(home, **stores)


class PlaneMemoryHome:
    """The plane's memory home, opened ONCE and kept for the service's lifetime (architect review C1).

    Opening a home (`MemoryFacade.open_mind`) touches the file's schema and so waits for any writer's lock
    (a per-request open stalled 1.9 s behind a 2 s writer and failed after 5 s behind a 7 s one); one open
    per service makes every later read a plain read. The home does not depend on the loaded bundles, so a
    host reload keeps it; the service closes it at shutdown (`service._stop_gateway_service_instance`). `get` answers
    None while the home does not exist (a read never creates it) and re-opens if the resolved path changes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Optional[Path] = None
        self._facade: Any = None

    def get(self, config: GatewayMemoryStoreConfig, **stores: Any) -> Optional[Any]:
        home = resolve_memory_home(config)
        with self._lock:
            if self._facade is not None and self._path == home:
                return self._facade
            if not home.exists():
                return None
            previous = self._facade
            self._facade = open_memory_home(config, create=False, **stores)
            self._path = home
        if previous is not None:
            _close_facade(previous)
        return self._facade

    def close(self) -> None:
        with self._lock:
            facade, self._facade, self._path = self._facade, None, None
        if facade is not None:
            _close_facade(facade)


def _close_facade(facade: Any) -> None:
    for part in (facade.journal, facade.store):
        close = getattr(part, "close", None)
        if callable(close):
            close()


def _memory_facade() -> Any:
    """The runtime's AbstractMemory facade module (runtime ADR 0011; gateway ADR 0001): the store
    classes are its re-exports, never a direct `abstractmemory` import."""
    from abstractruntime.integrations.abstractmemory import memory_facade

    return memory_facade


def memory_store_exists(config: GatewayMemoryStoreConfig) -> bool:
    if config.backend == "memory":
        return True
    if config.path is None:
        return False
    return Path(config.path).exists()


def memory_backend_unavailable_reason(backend: str) -> Optional[str]:
    """Return a human-readable reason when the selected AbstractMemory backend is unavailable."""

    resolved_backend = _normalize_backend(backend)
    class_name = _BACKEND_CLASS_NAMES[resolved_backend]
    try:
        memory_facade = _memory_facade()
    except ImportError as e:
        # The runtime predates its AbstractMemory facade: name the runtime floor (as the host-build
        # RuntimeTooOld check does), not AbstractMemory.
        from .live_deltas import ABSTRACTRUNTIME_FLOOR

        return (
            f"this gateway needs abstractruntime>={ABSTRACTRUNTIME_FLOOR} (its AbstractMemory facade, "
            "abstractruntime.integrations.abstractmemory.memory_facade); the installed runtime lacks it. "
            f"Upgrade it: pip install -U 'abstractruntime>={ABSTRACTRUNTIME_FLOOR}'. Import error: {e}"
        )
    try:
        store_class = getattr(memory_facade, class_name, None)
    except Exception as e:  # the facade's install hint: AbstractMemory itself is missing or too old
        return (
            "AbstractMemory is not available (the gateway needs AbstractMemory>=0.4.0 through AbstractRuntime). "
            f"Install/repair the base Gateway environment with `pip install abstractgateway`. Import error: {e}"
        )

    if store_class is None:
        return (
            f"Memory backend '{resolved_backend}' requires AbstractMemory class `{class_name}`, "
            "but the installed AbstractMemory package does not expose it. Install a compatible "
            "AbstractMemory build for that backend or choose `lancedb`/`memory`."
        )

    if resolved_backend == "lancedb":
        try:
            import lancedb  # noqa: F401
        except Exception as e:
            return (
                "Memory backend 'lancedb' requires the `lancedb` package. "
                "Install/repair with `pip install abstractgateway` or `pip install AbstractMemory[lancedb]`. "
                f"Import error: {e}"
            )

    return None


def _memory_store_class(backend: str) -> Any:
    resolved_backend = _normalize_backend(backend)
    reason = memory_backend_unavailable_reason(resolved_backend)
    if reason:
        raise RuntimeError(reason)

    return getattr(_memory_facade(), _BACKEND_CLASS_NAMES[resolved_backend])


def _normalize_embedder(embedder: Optional[Any]) -> Optional[Any]:
    if embedder is None or not callable(getattr(embedder, "embed_texts", None)):
        return embedder

    class _EmbeddingAdapter:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            # Identity rides the wrap: the engine's embedder_model_id()
            # reads `model`/`model_id`; an anonymous adapter over a named
            # embedder would silently disable identity-vs-pin enforcement.
            inner_model = getattr(inner, "model", None) or getattr(inner, "model_id", None)
            if isinstance(inner_model, str) and inner_model.strip():
                self.model = inner_model.strip()

        def embed_texts(self, texts: Any) -> Any:
            result = self._inner.embed_texts(texts)
            return getattr(result, "embeddings", result)

    return _EmbeddingAdapter(embedder)


def build_gateway_memory_embedder(*, base_dir: Path) -> Optional[Any]:
    """Build the Gateway embedding adapter used by vector-capable KG stores.

    Embeddings are optional. Returning None keeps structured KG memory usable
    while semantic retrieval reports a clear capability limitation.
    """

    try:
        from .embeddings_config import build_embedding_client, resolve_embedding_config

        embedding_route = resolve_embedding_config(base_dir=Path(base_dir))
        emb_client = build_embedding_client(
            embedding_route,
            cache_dir=Path(base_dir) / "abstractcore" / "embeddings",
            strict=True,
        )

        class _Embedder:
            def __init__(self, client: Any, model: str = "") -> None:
                self._client = client
                # Embedding-space identity (plan item 3 / memory M1): the
                # engine's `embedder_model_id()` reads a `model` attribute to
                # enforce identity-vs-pin at open. Without this the HTTP
                # wrapper was anonymous and only DIMENSION enforcement could
                # fire for gateway-opened homes.
                if model:
                    self.model = model

            def embed_texts(self, texts: Any) -> Any:
                return self._client.embed_texts(texts).embeddings

        return _normalize_embedder(_Embedder(emb_client, model=str(embedding_route.model or "")))
    except Exception:
        return None


def open_gateway_memory_store(
    *,
    base_dir: Path,
    embedder: Optional[Any] = None,
    backend: Optional[str] = None,
    path: Optional[Path | str] = None,
    require_vector: Optional[bool] = None,
    with_journal: bool = False,
) -> GatewayMemoryStoreResolution:
    """Open an AbstractMemory TripleStore selected by Gateway config.

    Gateway owns backend/path selection. AbstractMemory owns the store classes
    and query semantics. `with_journal`: with the `sqlite` backend also open the
    `SQLiteJournal` on the same file (the plane home's journal), so MEMORY_REVISE
    closures are journaled; other backends have no journal (None).
    """

    config = resolve_memory_store_config(
        base_dir=base_dir,
        backend=backend,
        path=path,
        require_vector=require_vector,
    )

    warnings: list[str] = []
    journal: Any = None
    store_embedder = _normalize_embedder(embedder)
    has_embedder = store_embedder is not None

    if config.backend == "lancedb":
        LanceDBTripleStore = _memory_store_class("lancedb")
        if config.path is None:
            raise RuntimeError("LanceDB memory backend requires a store path")
        config.path.parent.mkdir(parents=True, exist_ok=True)
        store = LanceDBTripleStore(config.path, embedder=store_embedder)
        capabilities = {
            "persistent": True,
            "structured_query": True,
            "vector_capable": True,
            "semantic_query": bool(has_embedder),
            "query_vector": True,
            "embedder_configured": bool(has_embedder),
        }
        if not has_embedder:
            warnings.append("Semantic KG queries require configured embeddings; structured queries remain available.")
    elif config.backend == "sqlite":
        SQLiteTripleStore = _memory_store_class("sqlite")
        if config.path is None:
            raise RuntimeError("SQLite memory backend requires a store path")
        config.path.parent.mkdir(parents=True, exist_ok=True)
        store = SQLiteTripleStore(config.path)
        if with_journal:
            journal = _memory_facade().SQLiteJournal(config.path)
        capabilities = {
            "persistent": True,
            "structured_query": True,
            "vector_capable": False,
            "semantic_query": False,
            "query_vector": False,
            "embedder_configured": bool(has_embedder),
        }
        warnings.append("SQLite KG memory is persistent but structured-only; semantic/vector queries are unavailable.")
    else:
        InMemoryTripleStore = _memory_store_class("memory")
        store = InMemoryTripleStore(embedder=store_embedder)
        capabilities = {
            "persistent": False,
            "structured_query": True,
            "vector_capable": bool(has_embedder),
            "semantic_query": bool(has_embedder),
            "query_vector": bool(has_embedder),
            "embedder_configured": bool(has_embedder),
        }
        warnings.append("In-memory KG store is process-local and non-durable; use only for tests/dev.")

    if config.require_vector and not bool(capabilities.get("semantic_query")):
        try:
            for part in (journal, store):
                close = getattr(part, "close", None)
                if callable(close):
                    close()
        finally:
            raise RuntimeError(
                f"Memory backend '{config.backend}' does not satisfy ABSTRACTGATEWAY_MEMORY_REQUIRE_VECTOR=1 "
                "with the current embedding configuration."
            )

    return GatewayMemoryStoreResolution(
        store=store,
        config=config,
        capabilities=capabilities,
        warnings=warnings,
        journal=journal,
    )
