from __future__ import annotations

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


def _gateway_capability_text_default() -> tuple[Optional[str], Optional[str], Optional[str]]:
    try:
        from .capability_defaults import gateway_capability_defaults_payload

        payload = gateway_capability_defaults_payload()
        routes = payload.get("routes") if isinstance(payload, dict) else None
        if not isinstance(routes, list):
            return None, None, None
        for kind in ("output", "input"):
            for route in routes:
                if not isinstance(route, dict):
                    continue
                route_kind = str(route.get("kind") or route.get("direction") or "").strip()
                if route_kind != kind:
                    continue
                if str(route.get("modality") or "").strip() != "text":
                    continue
                provider = _clean_provider(route.get("provider"))
                model = _clean_model(route.get("model"))
                if provider and model:
                    return provider, model, str(route.get("source") or payload.get("authority") or "abstractcore_config")
    except Exception:
        pass
    return None, None, None


def provider_model_config_error(*, purpose: str = "LLM helper") -> str:
    return (
        f"No provider/model is configured for {purpose}. Provide provider and model in the request, "
        "supply workflow defaults, or configure the execution-host output.text capability default."
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

    if provider_s or model_s:
        if provider_s and model_s:
            return ProviderModelResolution(provider=provider_s, model=model_s, source=source)
        return ProviderModelResolution(
            provider=provider_s,
            model=model_s,
            source=source,
            error=provider_model_config_error(purpose=purpose),
        )

    if flow_defaults:
        flow_provider = _clean_provider(flow_defaults[0])
        flow_model = _clean_model(flow_defaults[1])
        if flow_provider and flow_model:
            provider_s, model_s = flow_provider, flow_model
            source = "flow_defaults"

    if not provider_s or not model_s:
        cfg_provider, cfg_model, cfg_source = _gateway_capability_text_default()
        cfg_provider = _clean_provider(cfg_provider)
        cfg_model = _clean_model(cfg_model)
        if cfg_provider and cfg_model:
            provider_s = cfg_provider
            model_s = cfg_model
            source = cfg_source or "abstractcore_config"

    if provider_s and model_s:
        return ProviderModelResolution(provider=provider_s, model=model_s, source=source or "resolved")
    return ProviderModelResolution(
        provider=provider_s,
        model=model_s,
        source=source,
        error=provider_model_config_error(purpose=purpose),
    )
