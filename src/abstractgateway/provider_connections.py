from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


@dataclass(frozen=True)
class BuiltinProviderConnectionSpec:
    provider_id: str
    display_name: str
    description: str
    api_key_attr: Optional[str] = None
    api_key_env_vars: tuple[str, ...] = ()
    base_url_env_vars: tuple[str, ...] = ()
    default_base_url: str = ""
    requires_api_key: bool = False
    requires_base_url: bool = False


BUILTIN_PROVIDER_CONNECTIONS: tuple[BuiltinProviderConnectionSpec, ...] = (
    BuiltinProviderConnectionSpec(
        provider_id="openai",
        display_name="OpenAI",
        description="OpenAI API or an OpenAI-compatible OpenAI deployment.",
        api_key_attr="openai",
        api_key_env_vars=("OPENAI_API_KEY",),
        base_url_env_vars=("OPENAI_BASE_URL",),
        default_base_url="https://api.openai.com/v1",
        requires_api_key=True,
    ),
    BuiltinProviderConnectionSpec(
        provider_id="anthropic",
        display_name="Anthropic",
        description="Anthropic Claude API or a Claude-compatible Anthropic proxy.",
        api_key_attr="anthropic",
        api_key_env_vars=("ANTHROPIC_API_KEY",),
        base_url_env_vars=("ANTHROPIC_BASE_URL",),
        default_base_url="https://api.anthropic.com/v1",
        requires_api_key=True,
    ),
    BuiltinProviderConnectionSpec(
        provider_id="openrouter",
        display_name="OpenRouter",
        description="OpenRouter account connection for multi-provider routing.",
        api_key_attr="openrouter",
        api_key_env_vars=("OPENROUTER_API_KEY",),
        base_url_env_vars=("OPENROUTER_BASE_URL",),
        default_base_url="https://openrouter.ai/api/v1",
        requires_api_key=True,
    ),
    BuiltinProviderConnectionSpec(
        provider_id="portkey",
        display_name="Portkey",
        description="Portkey gateway connection for governed provider routing.",
        api_key_attr="portkey",
        api_key_env_vars=("PORTKEY_API_KEY",),
        base_url_env_vars=("PORTKEY_BASE_URL",),
        default_base_url="https://api.portkey.ai/v1",
        requires_api_key=True,
    ),
    BuiltinProviderConnectionSpec(
        provider_id="lmstudio",
        display_name="LM Studio",
        description="Local or remote LM Studio server.",
        base_url_env_vars=("LMSTUDIO_BASE_URL",),
        default_base_url="http://localhost:1234/v1",
        requires_base_url=True,
    ),
    BuiltinProviderConnectionSpec(
        provider_id="ollama",
        display_name="Ollama",
        description="Local or remote Ollama server.",
        base_url_env_vars=("OLLAMA_BASE_URL", "OLLAMA_HOST"),
        default_base_url="http://localhost:11434",
        requires_base_url=True,
    ),
    BuiltinProviderConnectionSpec(
        provider_id="openai-compatible",
        display_name="OpenAI-compatible",
        description="Generic OpenAI-compatible /v1 endpoint configured through OPENAI_BASE_URL.",
        api_key_attr="openai_compatible",
        api_key_env_vars=("OPENAI_API_KEY",),
        base_url_env_vars=("OPENAI_BASE_URL",),
        requires_base_url=True,
    ),
)


def builtin_provider_connection_specs() -> tuple[BuiltinProviderConnectionSpec, ...]:
    return BUILTIN_PROVIDER_CONNECTIONS


def configured_builtin_provider_public_rows(
    *,
    current_base_dir: Path,
    root_base_dir: Path,
    skip_profile_ids: Optional[Iterable[str]] = None,
) -> list[Dict[str, Any]]:
    """Return direct Core providers already usable by config/env.

    These rows are transient control-plane views. They do not duplicate secrets
    into Gateway endpoint profiles, and they deliberately expose direct provider
    ids (for example ``anthropic``), not virtual ``endpoint:*`` ids.
    """

    skip = {str(value or "").strip().lower() for value in (skip_profile_ids or []) if str(value or "").strip()}
    rows: list[Dict[str, Any]] = []
    for spec in BUILTIN_PROVIDER_CONNECTIONS:
        if spec.provider_id.lower() in skip:
            continue
        api_key, api_key_source = configured_provider_api_key(
            spec.provider_id,
            current_base_dir=current_base_dir,
            root_base_dir=root_base_dir,
        )
        base_url, base_url_source = configured_provider_base_url(spec.provider_id)
        if spec.provider_id == "openai-compatible" and _is_openai_base_url(base_url):
            continue
        if spec.requires_api_key and not api_key:
            continue
        if spec.requires_base_url and not base_url:
            continue

        row = builtin_provider_public_row(
            spec.provider_id,
            api_key=api_key,
            base_url=base_url,
            source=api_key_source or base_url_source or "abstractcore.config",
        )
        if row:
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("display_name") or row.get("provider_id") or "").lower())
    return rows


def builtin_provider_public_row(
    provider_id: str,
    *,
    api_key: str = "",
    base_url: str = "",
    source: str = "abstractcore.config",
    discovered_models: Optional[Iterable[str]] = None,
) -> Optional[Dict[str, Any]]:
    spec = _spec_for_provider(provider_id)
    if spec is None:
        return None
    models = [str(value).strip() for value in (discovered_models or []) if str(value or "").strip()]
    models = sorted(set(models))
    return {
        "id": spec.provider_id,
        "provider_id": spec.provider_id,
        "display_name": spec.display_name,
        "description": spec.description,
        "provider_family": spec.provider_id,
        "base_url": str(base_url or "").strip().rstrip("/"),
        "default_base_url": spec.default_base_url,
        "base_url_configured": bool(str(base_url or "").strip()),
        "api_key_set": bool(str(api_key or "").strip()),
        "api_key_fingerprint": _fingerprint(api_key),
        "scope": _public_scope_label(source),
        "capabilities": ["text"],
        "allowed_models": [],
        "discovered_model_count": len(models),
        "enabled": True,
        "managed": False,
        "synthetic": True,
        "source": source,
    }


def configured_provider_request_kwargs(
    provider_id: str,
    *,
    current_base_dir: Path,
    root_base_dir: Path,
    include_env: bool = True,
) -> Dict[str, Any]:
    """Return safe direct-provider kwargs for internal Core calls.

    Raw values are for in-process execution only and must not be serialized to a
    browser response.
    """

    out: Dict[str, Any] = {}
    key, _key_source = configured_provider_api_key(
        provider_id,
        current_base_dir=current_base_dir,
        root_base_dir=root_base_dir,
        include_env=include_env,
    )
    if key:
        out["api_key"] = key
    base_url, _base_source = configured_provider_base_url(provider_id)
    if base_url:
        out["base_url"] = base_url
    return out


def configured_provider_api_key(
    provider_id: str,
    *,
    current_base_dir: Path,
    root_base_dir: Path,
    include_env: bool = True,
) -> tuple[str, str]:
    spec = _spec_for_provider(provider_id)
    if spec is None or not spec.api_key_attr:
        return "", ""

    config_paths = _scoped_core_config_paths(current_base_dir=current_base_dir, root_base_dir=root_base_dir)
    for path, scope in config_paths:
        key = _api_key_from_core_config(path, spec.api_key_attr)
        if key:
            return key, scope

    if include_env:
        key = _env_first(spec.api_key_env_vars)
        if key:
            return key, "environment"
    return "", ""


def configured_provider_base_url(provider_id: str) -> tuple[str, str]:
    spec = _spec_for_provider(provider_id)
    if spec is None:
        return "", ""
    value = _env_first(spec.base_url_env_vars)
    if value:
        return value.rstrip("/"), "environment"
    return "", ""


def _scoped_core_config_paths(*, current_base_dir: Path, root_base_dir: Path) -> list[tuple[Path, str]]:
    current = Path(current_base_dir).expanduser().resolve()
    root = Path(root_base_dir).expanduser().resolve()
    paths: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path, label: str) -> None:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        paths.append((resolved, label))

    # Per-runtime config must win over the Gateway baseline in user-auth mode.
    add(current / "config" / "abstractcore.json", "user")
    if root != current:
        add(root / "config" / "abstractcore.json", "gateway")

    try:
        from .users import gateway_user_auth_enabled

        user_auth = bool(gateway_user_auth_enabled())
    except Exception:
        user_auth = False

    if not user_auth:
        try:
            from abstractcore.config.manager import ConfigurationManager

            manager = ConfigurationManager(apply_env=False)
            add(Path(manager.config_file), "abstractcore.config")
        except Exception:
            pass
    return paths


def _api_key_from_core_config(path: Path, attr: str) -> str:
    if not Path(path).exists():
        return ""
    try:
        from abstractcore.config.manager import ConfigurationManager

        manager = ConfigurationManager(config_file=path, apply_env=False)
        value = getattr(manager.config.api_keys, attr, None)
        return str(value or "").strip()
    except Exception:
        return ""


def _env_first(keys: Iterable[str]) -> str:
    for key in keys:
        value = os.getenv(str(key))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _spec_for_provider(provider_id: str) -> Optional[BuiltinProviderConnectionSpec]:
    wanted = str(provider_id or "").strip().lower()
    for spec in BUILTIN_PROVIDER_CONNECTIONS:
        if spec.provider_id == wanted:
            return spec
    return None


def _fingerprint(secret: str) -> str:
    value = str(secret or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _is_openai_base_url(value: str) -> bool:
    return "api.openai.com" in str(value or "").strip().lower()


def _public_scope_label(source: str) -> str:
    value = str(source or "").strip().lower()
    if value in {"user", "gateway", "environment"}:
        return value
    return "core"
