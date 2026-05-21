from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple


class ProviderModelConfigError(ValueError):
    """Raised when an LLM helper cannot resolve a provider/model pair."""


@dataclass(frozen=True)
class ProviderModelResolution:
    provider: Optional[str]
    model: Optional[str]
    source: Optional[str]
    error: Optional[str] = None

    def require(self) -> tuple[str, str]:
        if self.provider and self.model:
            return self.provider, self.model
        raise ProviderModelConfigError(self.error or provider_model_config_error())


def _clean_provider(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text or None


def _clean_model(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _env_first(*keys: str) -> Optional[str]:
    for key in keys:
        raw = os.getenv(str(key))
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def provider_model_config_error(*, purpose: str = "LLM helper") -> str:
    return (
        f"No provider/model is configured for {purpose}. Provide provider and model in the request, "
        "set ABSTRACTGATEWAY_PROVIDER and ABSTRACTGATEWAY_MODEL, or supply flow defaults."
    )


def resolve_gateway_provider_model(
    *,
    provider: Any = None,
    model: Any = None,
    flow_defaults: Optional[Tuple[str, str]] = None,
    purpose: str = "LLM helper",
) -> ProviderModelResolution:
    """Resolve the provider/model cascade used by Gateway LLM helper paths."""

    provider_s = _clean_provider(provider)
    model_s = _clean_model(model)
    source: Optional[str] = "request" if provider_s or model_s else None

    env_provider = _clean_provider(_env_first("ABSTRACTGATEWAY_PROVIDER", "ABSTRACTFLOW_PROVIDER"))
    env_model = _clean_model(_env_first("ABSTRACTGATEWAY_MODEL", "ABSTRACTFLOW_MODEL"))
    if not provider_s and env_provider:
        provider_s = env_provider
        source = "gateway_env"
    if not model_s and env_model:
        model_s = env_model
        source = "gateway_env"

    if flow_defaults and (not provider_s or not model_s):
        flow_provider = _clean_provider(flow_defaults[0])
        flow_model = _clean_model(flow_defaults[1])
        if flow_provider and flow_model:
            if not provider_s and not model_s:
                provider_s, model_s = flow_provider, flow_model
                source = "flow_defaults"
            elif provider_s and not model_s and provider_s == flow_provider:
                model_s = flow_model
                source = source or "flow_defaults"
            elif model_s and not provider_s and model_s == flow_model:
                provider_s = flow_provider
                source = source or "flow_defaults"

    if provider_s and model_s:
        return ProviderModelResolution(provider=provider_s, model=model_s, source=source or "resolved")
    return ProviderModelResolution(
        provider=provider_s,
        model=model_s,
        source=source,
        error=provider_model_config_error(purpose=purpose),
    )
