from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse


VIRTUAL_PROVIDER_PREFIX = "endpoint:"

_SAFE_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SAFE_PROVIDER_FAMILY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_DEFAULT_CAPABILITIES = ("text",)
_MAX_SECRET_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProviderEndpointProfile:
    id: str
    display_name: str
    description: str = ""
    provider_family: str = "openai-compatible"
    base_url: str = ""
    api_key: str = ""
    scope: str = "user"
    capabilities: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_CAPABILITIES)
    allowed_models: tuple[str, ...] = ()
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    @property
    def virtual_provider_id(self) -> str:
        return virtual_provider_id(self.id)

    @property
    def api_key_fingerprint(self) -> str:
        key = str(self.api_key or "").strip()
        if not key:
            return ""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "virtual_provider": self.virtual_provider_id,
            "display_name": self.display_name or self.id,
            "description": self.description,
            "provider_family": self.provider_family,
            "base_url": self.base_url,
            "base_url_configured": bool(self.base_url),
            "api_key_set": bool(str(self.api_key or "").strip()),
            "api_key_fingerprint": self.api_key_fingerprint,
            "scope": self.scope,
            "capabilities": list(self.capabilities),
            "allowed_models": list(self.allowed_models),
            "enabled": bool(self.enabled),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def private_resolution(self) -> Dict[str, Any]:
        out = self.public_dict()
        out["provider"] = self.provider_family
        out["api_key"] = self.api_key
        return out


class ProviderEndpointProfileError(ValueError):
    pass


class ProviderEndpointProfileStore:
    def __init__(self, *, base_dir: Path):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.path = (self.base_dir / "config" / "provider_endpoint_profiles.json").resolve()
        self._lock = threading.Lock()

    def list_profiles(self) -> List[ProviderEndpointProfile]:
        with self._lock:
            return list(self._load_profiles_locked())

    def get_profile(self, profile_id: str) -> Optional[ProviderEndpointProfile]:
        wanted = normalize_profile_id(profile_id)
        for profile in self.list_profiles():
            if profile.id.lower() == wanted.lower():
                return profile
        return None

    def upsert_profile(
        self,
        *,
        profile_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        provider_family: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        clear_api_key: bool = False,
        scope: Optional[str] = None,
        capabilities: Optional[Iterable[str]] = None,
        allowed_models: Optional[Iterable[str]] = None,
        enabled: Optional[bool] = None,
    ) -> ProviderEndpointProfile:
        profile_id = normalize_profile_id(profile_id)
        now = utc_now_iso()
        with self._lock:
            profiles = self._load_profiles_locked()
            existing = next((p for p in profiles if p.id.lower() == profile_id.lower()), None)
            if existing is None:
                created_at = now
                current_key = ""
            else:
                created_at = existing.created_at or now
                current_key = existing.api_key

            if api_key is not None:
                api_key_value = normalize_api_key(api_key)
            elif clear_api_key:
                api_key_value = ""
            else:
                api_key_value = current_key

            profile = ProviderEndpointProfile(
                id=profile_id,
                display_name=normalize_display_name(display_name if display_name is not None else (existing.display_name if existing else profile_id)),
                description=normalize_description(description if description is not None else (existing.description if existing else "")),
                provider_family=normalize_provider_family(provider_family if provider_family is not None else (existing.provider_family if existing else "openai-compatible")),
                base_url=normalize_base_url(base_url if base_url is not None else (existing.base_url if existing else "")),
                api_key=api_key_value,
                scope=normalize_scope(scope if scope is not None else (existing.scope if existing else "user")),
                capabilities=normalize_string_list(capabilities if capabilities is not None else (existing.capabilities if existing else _DEFAULT_CAPABILITIES), default=_DEFAULT_CAPABILITIES),
                allowed_models=normalize_string_list(allowed_models if allowed_models is not None else (existing.allowed_models if existing else ()), default=()),
                enabled=bool(enabled) if enabled is not None else (bool(existing.enabled) if existing is not None else True),
                created_at=created_at,
                updated_at=now,
            )

            next_profiles = [p for p in profiles if p.id.lower() != profile_id.lower()]
            next_profiles.append(profile)
            next_profiles.sort(key=lambda p: (p.scope, p.display_name.lower(), p.id.lower()))
            self._save_profiles_locked(next_profiles)
            return profile

    def delete_profile(self, profile_id: str) -> bool:
        wanted = normalize_profile_id(profile_id)
        with self._lock:
            profiles = self._load_profiles_locked()
            next_profiles = [p for p in profiles if p.id.lower() != wanted.lower()]
            if len(next_profiles) == len(profiles):
                return False
            self._save_profiles_locked(next_profiles)
            return True

    def _load_profiles_locked(self) -> List[ProviderEndpointProfile]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
            obj = json.loads(raw)
        except Exception as exc:
            raise ProviderEndpointProfileError(f"Failed to read provider endpoint profiles: {exc}") from exc
        rows = obj.get("profiles") if isinstance(obj, dict) else None
        if not isinstance(rows, list):
            return []

        profiles: List[ProviderEndpointProfile] = []
        for raw_profile in rows:
            if not isinstance(raw_profile, dict):
                continue
            try:
                profile = ProviderEndpointProfile(
                    id=normalize_profile_id(raw_profile.get("id")),
                    display_name=normalize_display_name(raw_profile.get("display_name") or raw_profile.get("name") or raw_profile.get("id")),
                    description=normalize_description(raw_profile.get("description")),
                    provider_family=normalize_provider_family(raw_profile.get("provider_family") or raw_profile.get("provider") or "openai-compatible"),
                    base_url=normalize_base_url(raw_profile.get("base_url")),
                    api_key=normalize_api_key(raw_profile.get("api_key")),
                    scope=normalize_scope(raw_profile.get("scope") or "user"),
                    capabilities=normalize_string_list(raw_profile.get("capabilities"), default=_DEFAULT_CAPABILITIES),
                    allowed_models=normalize_string_list(raw_profile.get("allowed_models"), default=()),
                    enabled=bool(raw_profile.get("enabled", True)),
                    created_at=normalize_timestamp(raw_profile.get("created_at")),
                    updated_at=normalize_timestamp(raw_profile.get("updated_at")),
                )
            except ProviderEndpointProfileError:
                continue
            profiles.append(profile)
        profiles.sort(key=lambda p: (p.scope, p.display_name.lower(), p.id.lower()))
        return profiles

    def _save_profiles_locked(self, profiles: List[ProviderEndpointProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for p in profiles:
            rows.append(
                {
                    "id": p.id,
                    "display_name": p.display_name,
                    "description": p.description,
                    "provider_family": p.provider_family,
                    "base_url": p.base_url,
                    "api_key": p.api_key,
                    "scope": p.scope,
                    "capabilities": list(p.capabilities),
                    "allowed_models": list(p.allowed_models),
                    "enabled": bool(p.enabled),
                    "created_at": p.created_at,
                    "updated_at": p.updated_at,
                }
            )
        data = json.dumps({"version": 1, "updated_at": utc_now_iso(), "profiles": rows}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    return text[:80]


def normalize_profile_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith(VIRTUAL_PROVIDER_PREFIX):
        text = text[len(VIRTUAL_PROVIDER_PREFIX) :]
    if not text or not _SAFE_PROFILE_ID_RE.match(text):
        raise ProviderEndpointProfileError("Endpoint profile id must start with a letter or number and contain only letters, numbers, dot, dash, or underscore.")
    return text


def normalize_display_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProviderEndpointProfileError("Endpoint profile name is required.")
    if len(text) > 120:
        raise ProviderEndpointProfileError("Endpoint profile name is too long (max 120 characters).")
    return text


def normalize_description(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 1000:
        raise ProviderEndpointProfileError("Endpoint profile description is too long (max 1000 characters).")
    return text


def normalize_provider_family(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "openai compatible": "openai-compatible",
        "openai_compatible": "openai-compatible",
        "openai-compatible": "openai-compatible",
    }
    text = aliases.get(text, text)
    if not text or not _SAFE_PROVIDER_FAMILY_RE.match(text):
        raise ProviderEndpointProfileError("Provider family must contain only letters, numbers, dot, dash, or underscore.")
    return text


def normalize_base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if len(text) > 2048:
        raise ProviderEndpointProfileError("Base URL is too long.")
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ProviderEndpointProfileError("Base URL must be an http(s) URL.")
    return text


def normalize_api_key(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if "\x00" in text:
        raise ProviderEndpointProfileError("API key contains an invalid NUL byte.")
    if len(text.encode("utf-8", errors="replace")) > _MAX_SECRET_BYTES:
        raise ProviderEndpointProfileError("API key is too large.")
    return text


def normalize_scope(value: Any) -> str:
    text = str(value or "user").strip().lower()
    if text not in {"user", "gateway"}:
        raise ProviderEndpointProfileError("Endpoint profile scope must be 'user' or 'gateway'.")
    return text


def normalize_string_list(value: Any, *, default: Iterable[str]) -> tuple[str, ...]:
    raw: Iterable[Any]
    if value is None:
        raw = list(default)
    elif isinstance(value, str):
        raw = re.split(r"[,;\n]", value)
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = list(default)
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 256:
            raise ProviderEndpointProfileError("List item is too long (max 256 characters).")
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    if not out and tuple(default):
        return tuple(str(x) for x in default if str(x).strip())
    return tuple(out)


def virtual_provider_id(profile_id: str) -> str:
    return f"{VIRTUAL_PROVIDER_PREFIX}{normalize_profile_id(profile_id)}"


def profile_id_from_virtual_provider(provider: Any) -> Optional[str]:
    text = str(provider or "").strip()
    if not text.startswith(VIRTUAL_PROVIDER_PREFIX):
        return None
    return normalize_profile_id(text[len(VIRTUAL_PROVIDER_PREFIX) :])


def effective_endpoint_profiles(*, base_dir: Path, root_base_dir: Optional[Path] = None) -> List[ProviderEndpointProfile]:
    current_store = ProviderEndpointProfileStore(base_dir=base_dir)
    current_profiles = current_store.list_profiles()
    root_profiles: List[ProviderEndpointProfile] = []
    if root_base_dir is not None and Path(root_base_dir).expanduser().resolve() != Path(base_dir).expanduser().resolve():
        root_profiles = [p for p in ProviderEndpointProfileStore(base_dir=root_base_dir).list_profiles() if p.scope == "gateway"]

    by_key: Dict[str, ProviderEndpointProfile] = {}
    for profile in root_profiles + current_profiles:
        by_key[profile.virtual_provider_id.lower()] = profile
    profiles = list(by_key.values())
    profiles.sort(key=lambda p: (0 if p.scope == "user" else 1, p.display_name.lower(), p.id.lower()))
    return profiles


def resolve_effective_endpoint_profile(provider: Any, *, base_dir: Path, root_base_dir: Optional[Path] = None) -> Optional[ProviderEndpointProfile]:
    profile_id = profile_id_from_virtual_provider(provider)
    if not profile_id:
        return None
    for profile in effective_endpoint_profiles(base_dir=base_dir, root_base_dir=root_base_dir):
        if profile.id.lower() == profile_id.lower() and profile.enabled:
            return profile
    return None
