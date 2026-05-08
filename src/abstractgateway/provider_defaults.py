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


def _lookup(raw: Any, *keys: str) -> Optional[str]:
    for key in keys:
        if isinstance(raw, dict):
            value = raw.get(key)
        else:
            value = getattr(raw, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _abstractcore_config_defaults() -> tuple[Optional[str], Optional[str]]:
    try:
        from abstractcore.config import get_config_manager  # type: ignore

        cfg = get_config_manager().config
    except Exception:
        return None, None

    default_models = getattr(cfg, "default_models", None)
    provider = _lookup(default_models, "global_provider", "provider", "default_provider")
    model = _lookup(default_models, "global_model", "model", "default_model")

    if not provider:
        provider = _lookup(cfg, "global_provider", "provider", "default_provider")
    if not model:
        model = _lookup(cfg, "global_model", "model", "default_model")

    return _clean_provider(provider), _clean_model(model)


def provider_model_config_error(*, purpose: str = "LLM helper") -> str:
    return (
        f"No provider/model is configured for {purpose}. Provide provider and model in the request, "
        "set ABSTRACTGATEWAY_PROVIDER and ABSTRACTGATEWAY_MODEL, or configure AbstractCore defaults "
        "with abstractcore-config."
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

    if not provider_s or not model_s:
        cfg_provider, cfg_model = _abstractcore_config_defaults()
        if cfg_provider and cfg_model:
            if not provider_s and not model_s:
                provider_s, model_s = cfg_provider, cfg_model
                source = "abstractcore_config"
            elif provider_s and not model_s and provider_s == cfg_provider:
                model_s = cfg_model
                source = source or "abstractcore_config"
            elif model_s and not provider_s and model_s == cfg_model:
                provider_s = cfg_provider
                source = source or "abstractcore_config"

    if provider_s and model_s:
        return ProviderModelResolution(provider=provider_s, model=model_s, source=source or "resolved")
    return ProviderModelResolution(
        provider=provider_s,
        model=model_s,
        source=source,
        error=provider_model_config_error(purpose=purpose),
    )
