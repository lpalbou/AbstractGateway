"""Gateway-side generations borrow the principal's runtime pool (R2, 2026-10-29 adversarial audit).

A run summary, Ask on a run, the backlog assist, the console sandbox and entity chat used to build
a `LocalAbstractCoreLLMClient` of their own per request: with an in-process engine (GGUF on
llama.cpp, transformers) that is a full extra copy of the model (24.2 GB in the model host child on
the audit's probe). They now call the principal's runtime pool, the client every run of that
account uses, with the per-call routing keys it understands (`_provider`, `_model`, `base_url`,
`api_key`), so they reuse the one resident instance: in this process, or in the model host child
(ADR 0002) when the pool is a proxy.

Only when the principal's runtime has no pool (a host whose bundles have no LLM node) is a client of
its own built, as before: there is no resident instance to share then.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The connection keys gateway resolution yields (`_gateway_provider_resolution`): an endpoint
# profile's or a console provider connection's base URL and key. The pool takes both per call.
_PER_CALL_KEYS = ("base_url", "api_key")


class PoolGeneration:
    """`generate(...)` on the runtime's pool for one provider/model (and its connection)."""

    def __init__(self, pool: Any, *, provider: str, model: str, llm_kwargs: Optional[Dict[str, Any]] = None) -> None:
        extra = sorted(k for k in (llm_kwargs or {}) if k not in _PER_CALL_KEYS)
        if extra:
            raise ValueError(f"the runtime pool takes only {', '.join(_PER_CALL_KEYS)} per call, not {', '.join(extra)}")
        self._pool = pool
        self._provider = str(provider or "").strip()
        self._model = str(model or "").strip()
        self._connection = {k: v for k, v in (llm_kwargs or {}).items() if v not in (None, "")}

    def _connection_beyond_the_pools(self) -> Dict[str, Any]:
        """The connection keys the pool does not already use for this provider: a key equal to the
        pool's own default connection would key a second client in the pool (another copy)."""
        default_provider = str(getattr(self._pool, "_default_provider", "") or "").strip().lower()
        if self._provider.lower() != default_provider:
            return dict(self._connection)
        own = getattr(self._pool, "_llm_kwargs", None) or {}
        return {k: v for k, v in self._connection.items() if own.get(k) != v}

    @property
    def pool(self) -> Any:
        return self._pool

    def generate(self, *, prompt: str = "", messages: Any = None, system_prompt: Any = None, tools: Any = None,
                 media: Any = None, params: Optional[Dict[str, Any]] = None) -> Any:
        p = dict(params or {})
        p["_provider"] = self._provider
        p["_model"] = self._model
        for key, value in self._connection_beyond_the_pools().items():
            p.setdefault(key, value)
        return self._pool.generate(prompt=prompt, messages=messages, system_prompt=system_prompt, tools=tools,
                                   media=media, params=p)


def principal_pool() -> Optional[Any]:
    """The current principal's runtime pool (its `_abstractcore_llm_client`), or None."""
    from .service import get_gateway_service

    try:
        svc = get_gateway_service()
    except Exception:  # noqa: BLE001 - no service (a bare test app): nothing to borrow
        return None
    runtime = getattr(getattr(svc, "host", None), "runtime", None)
    pool = getattr(runtime, "_abstractcore_llm_client", None)
    return pool if pool is not None and callable(getattr(pool, "generate", None)) else None


def generation_client(*, provider: str, model: str, llm_kwargs: Optional[Dict[str, Any]] = None,
                      local_ctor: Optional[Dict[str, Any]] = None) -> Any:
    """The client a gateway-side generation calls: the principal's pool when there is one, else a
    client of its own (`model_host.local_client(**local_ctor)`, or provider/model/llm_kwargs)."""
    pool = principal_pool()
    if pool is not None:
        return PoolGeneration(pool, provider=provider, model=model, llm_kwargs=llm_kwargs)
    from . import model_host

    ctor = dict(local_ctor or {"provider": provider, "model": model, "llm_kwargs": dict(llm_kwargs or {}) or None})
    logger.info("no runtime pool for this principal: %s/%s gets a client of its own", provider, model)
    return model_host.local_client(**ctor)
