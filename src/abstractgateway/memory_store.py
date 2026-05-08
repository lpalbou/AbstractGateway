from __future__ import annotations

import os
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


@dataclass(frozen=True)
class GatewayMemoryStoreConfig:
    backend: str
    path: Optional[Path]
    require_vector: bool = False

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
    )


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
        import abstractmemory  # type: ignore
    except Exception as e:  # pragma: no cover
        return (
            "AbstractMemory is not available. Install `abstractgateway[memory]` "
            f"or `AbstractMemory` in the same environment. Import error: {e}"
        )

    if getattr(abstractmemory, class_name, None) is None:
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
                "Install `abstractgateway[memory]` or `AbstractMemory[lancedb]`. "
                f"Import error: {e}"
            )

    return None


def _memory_store_class(backend: str) -> Any:
    resolved_backend = _normalize_backend(backend)
    reason = memory_backend_unavailable_reason(resolved_backend)
    if reason:
        raise RuntimeError(reason)

    import abstractmemory  # type: ignore

    return getattr(abstractmemory, _BACKEND_CLASS_NAMES[resolved_backend])


def _normalize_embedder(embedder: Optional[Any]) -> Optional[Any]:
    if embedder is None or not callable(getattr(embedder, "embed_texts", None)):
        return embedder

    class _EmbeddingAdapter:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

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
        from abstractruntime.integrations.abstractcore.embeddings_client import AbstractCoreEmbeddingsClient

        from .embeddings_config import resolve_embedding_config

        emb_provider, emb_model = resolve_embedding_config(base_dir=Path(base_dir))
        emb_client = AbstractCoreEmbeddingsClient(
            provider=str(emb_provider).strip().lower(),
            model=str(emb_model).strip(),
            manager_kwargs={
                "cache_dir": Path(base_dir) / "abstractcore" / "embeddings",
                "strict": True,
            },
        )

        class _Embedder:
            def __init__(self, client: Any) -> None:
                self._client = client

            def embed_texts(self, texts: Any) -> Any:
                return self._client.embed_texts(texts).embeddings

        return _normalize_embedder(_Embedder(emb_client))
    except Exception:
        return None


def open_gateway_memory_store(
    *,
    base_dir: Path,
    embedder: Optional[Any] = None,
    backend: Optional[str] = None,
    path: Optional[Path | str] = None,
    require_vector: Optional[bool] = None,
) -> GatewayMemoryStoreResolution:
    """Open an AbstractMemory TripleStore selected by Gateway config.

    Gateway owns backend/path selection. AbstractMemory owns the store classes
    and query semantics.
    """

    config = resolve_memory_store_config(
        base_dir=base_dir,
        backend=backend,
        path=path,
        require_vector=require_vector,
    )

    warnings: list[str] = []
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
            close = getattr(store, "close", None)
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
    )
