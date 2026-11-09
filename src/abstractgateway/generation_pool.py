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


class StructuredOutputMissing(ValueError):
    """A structured call (a response model) whose answer carried no parsed object."""


def structured_generate(generate: Any, kwargs: Dict[str, Any], *, provider: str, model: str) -> Any:
    """One `generate(**kwargs)` on a runtime client (the pool, or a client of its own), with
    STRUCTURED OUTPUT the way a direct AbstractCore client takes it (live blocker 2026-11-09: every
    visit-formed record went pending because the pooled client refused `response_model`).

    - `response_model` (a pydantic class): its JSON schema rides `params.response_schema` /
      `response_schema_name` (the runtime clients' wire; a proxy cannot carry a validator) and the
      answer comes back as that class's instance, as a direct client returns it.
    - `response_schema` / `response_schema_name` (a JSON schema): forwarded as is; the answer keeps
      the runtime's shape (`data` holds the parsed object).
    A structured answer without a parsed object raises StructuredOutputMissing naming the model."""
    kw = dict(kwargs)
    response_model = kw.pop("response_model", None)
    schema = kw.pop("response_schema", None)
    schema_name = kw.pop("response_schema_name", None)
    if response_model is not None:
        schema = response_model.model_json_schema()
        schema_name = schema_name or getattr(response_model, "__name__", None)
    if schema is not None:
        params = dict(kw.get("params") or {})
        params["response_schema"] = schema
        if schema_name:
            params["response_schema_name"] = str(schema_name)
        kw["params"] = params
    out = generate(**kw)
    if response_model is None:
        return out
    if hasattr(out, "model_dump"):
        return out
    data = out.get("data") if isinstance(out, dict) else getattr(out, "data", None)
    if not isinstance(data, dict):
        raise StructuredOutputMissing(
            f"the structured answer from {provider}/{model} carried no parsed object"
        )
    return response_model.model_validate(data)


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
        # Who answers (the authoring failure lines name it; never None).
        self.model_info = {"provider": self._provider, "model": self._model, "thinking": None}

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
                 media: Any = None, params: Optional[Dict[str, Any]] = None, response_model: Any = None,
                 response_schema: Any = None, response_schema_name: Optional[str] = None) -> Any:
        """The pool's generate for this provider/model; structured output (`response_model`, or
        `response_schema` + name) is carried as a direct client would (`structured_generate`)."""
        p = dict(params or {})
        p["_provider"] = self._provider
        p["_model"] = self._model
        for key, value in self._connection_beyond_the_pools().items():
            p.setdefault(key, value)
        kwargs: Dict[str, Any] = {"prompt": prompt, "messages": messages, "system_prompt": system_prompt,
                                  "tools": tools, "media": media, "params": p}
        if response_model is not None:
            kwargs["response_model"] = response_model
        if response_schema is not None:
            kwargs["response_schema"] = response_schema
            kwargs["response_schema_name"] = response_schema_name
        return structured_generate(self._pool.generate, kwargs, provider=self._provider, model=self._model)


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
                      local_ctor: Optional[Dict[str, Any]] = None,
                      patience: Optional[Dict[str, Any]] = None) -> Any:
    """The client a gateway-side generation calls: the principal's pool when there is one, else a
    client of its own (`model_host.local_client(**local_ctor)`, or provider/model/llm_kwargs).

    `patience` (`timeout`, `retry_wall_clock_budget_s`, `read_idle_timeout_s` for AbstractCore's
    `create_llm`) applies to a client built of its own; a borrowed pool keeps the account's own
    patience (the pool takes only a connection per call, never client construction kwargs)."""
    pool = principal_pool()
    if pool is not None:
        return PoolGeneration(pool, provider=provider, model=model, llm_kwargs=llm_kwargs)
    from . import model_host

    ctor = dict(local_ctor or {"provider": provider, "model": model, "llm_kwargs": dict(llm_kwargs or {}) or None})
    if patience:
        own_kwargs = dict(ctor.get("llm_kwargs") or {})
        own_kwargs.update({k: v for k, v in patience.items() if v is not None})
        ctor["llm_kwargs"] = own_kwargs
    logger.info("no runtime pool for this principal: %s/%s gets a client of its own", provider, model)
    return model_host.local_client(**ctor)
