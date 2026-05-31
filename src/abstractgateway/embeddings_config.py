from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence


@dataclass(frozen=True)
class EmbeddingRouteConfig:
    provider: str
    model: str
    base_url: Optional[str] = None
    source: str = "abstractcore.capability_defaults"
    authority: str = "abstractcore.local"
    remote_core_url: Optional[str] = None
    remote_core_token: Optional[str] = None


def resolve_embedding_config(*, base_dir: Path) -> EmbeddingRouteConfig:
    """Resolve the execution-host text embedding route.

    Gateway does not own embedding provider/model persistence. It reads the
    execution host's ``embedding.text`` capability route and either uses that
    route locally (embedded mode) or proxies embedding calls to the configured
    AbstractCore server (split mode).
    """

    from . import capability_defaults

    payload = capability_defaults.gateway_capability_defaults_payload(base_dir=base_dir)
    route = _embedding_text_route(payload)
    if route is None:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        suffix = f" Errors: {errors}" if errors else ""
        raise RuntimeError(
            "No execution-host embedding.text capability default is configured. "
            "Set it with `abstractgateway-config set-default embedding.text --provider ... --model ...` "
            "or configure it through the AbstractFlow defaults panel."
            + suffix
        )

    provider = str(route.get("provider") or "").strip().lower()
    model = str(route.get("model") or "").strip()
    if not provider or not model:
        raise RuntimeError("The execution-host embedding.text route must define both provider and model.")

    remote_core_url = capability_defaults.core_server_base_url() or None
    return EmbeddingRouteConfig(
        provider=provider,
        model=model,
        base_url=_clean(route.get("base_url")),
        source=str(route.get("source") or "abstractcore.capability_defaults"),
        authority=str(payload.get("authority") or "abstractcore.local") if isinstance(payload, dict) else "abstractcore.local",
        remote_core_url=remote_core_url,
        remote_core_token=capability_defaults.core_server_token() if remote_core_url else None,
    )


def build_embedding_client(route: EmbeddingRouteConfig, *, cache_dir: Path, strict: bool = True) -> Any:
    if route.remote_core_url:
        return RemoteCoreEmbeddingsClient(route)

    from abstractruntime.integrations.abstractcore.embeddings_client import AbstractCoreEmbeddingsClient

    manager_kwargs: Dict[str, Any] = {
        "cache_dir": cache_dir,
        "strict": bool(strict),
    }
    if route.base_url:
        manager_kwargs["provider_kwargs"] = {"base_url": route.base_url}

    return AbstractCoreEmbeddingsClient(
        provider=route.provider,
        model=route.model,
        manager_kwargs=manager_kwargs,
    )


class RemoteCoreEmbeddingsClient:
    """Embedding client that delegates generation to a remote AbstractCore server."""

    def __init__(self, route: EmbeddingRouteConfig) -> None:
        if not route.remote_core_url:
            raise ValueError("remote_core_url is required")
        self._route = route

    @property
    def provider(self) -> str:
        return self._route.provider

    @property
    def model(self) -> str:
        return self._route.model

    @property
    def base_url(self) -> Optional[str]:
        return self._route.base_url

    def embed_texts(self, texts: Sequence[str]) -> Any:
        from abstractruntime.integrations.abstractcore.embeddings_client import EmbeddingsResult

        from .capability_defaults import core_server_url

        payload: Dict[str, Any] = {
            "model": f"{self._route.provider}/{self._route.model}",
            "input": [str(text or "") for text in texts],
            "encoding_format": "float",
        }
        if self._route.base_url:
            payload["base_url"] = self._route.base_url

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._route.remote_core_token:
            headers["Authorization"] = f"Bearer {self._route.remote_core_token}"

        request = urllib.request.Request(
            core_server_url(str(self._route.remote_core_url), "/embeddings"),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120.0) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"AbstractCore embeddings route returned HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"AbstractCore embeddings route unavailable: {exc}") from exc

        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("AbstractCore embeddings route returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError("AbstractCore embeddings route returned a non-object payload")

        data = body.get("data")
        if not isinstance(data, list):
            raise RuntimeError("AbstractCore embeddings route returned no data list")
        ordered = sorted((item for item in data if isinstance(item, dict)), key=lambda item: int(item.get("index") or 0))
        embeddings = [list(item.get("embedding") or []) for item in ordered]
        dimension = len(embeddings[0]) if embeddings else 0
        return EmbeddingsResult(
            provider=self._route.provider,
            model=self._route.model,
            embeddings=embeddings,
            dimension=dimension,
        )


def _embedding_text_route(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list):
        return None
    for item in routes:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        kind = str(item.get("kind") or item.get("direction") or "").strip().lower()
        modality = str(item.get("modality") or "").strip().lower()
        if key == "embedding.text" or (kind == "embedding" and modality == "text"):
            if item.get("configured") or item.get("provider") or item.get("model"):
                return item
    return None


def _clean(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None
