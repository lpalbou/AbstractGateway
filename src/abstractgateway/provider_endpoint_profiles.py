from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
import uuid
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


def _gateway_root_data_dir() -> Optional[Path]:
    try:
        from .users import gateway_data_dir_from_env

        return gateway_data_dir_from_env()
    except Exception:
        return None


def _is_gateway_root(base_dir: Path) -> bool:
    root = _gateway_root_data_dir()
    if root is None:
        return False
    try:
        return Path(base_dir).expanduser().resolve() == Path(root).expanduser().resolve()
    except Exception:
        return False


class ProviderEndpointProfileStore:
    """The Gateway's view of provider endpoint profiles for ONE scope.

    ONE STORE FOR PROVIDER CONFIG (operator ruling 2026-08-01). At the gateway
    ROOT there is no Gateway file: a profile is provider configuration, so the
    root store IS AbstractCore's `provider_profiles` -- reached through
    `core_config`, the one door -- and a profile created from the Gateway
    console is the same row `abstractcore` and its console-TUI see. Only a
    per-USER runtime keeps a file of its own, because a user's private profile
    has no Core representation and must not leak into the host's store.

    `core_backed` is derived, never configured: the root is whatever
    `ABSTRACTGATEWAY_DATA_DIR` resolves to. Pass it explicitly only in tests.
    """

    def __init__(self, *, base_dir: Path, core_backed: Optional[bool] = None):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.path = (self.base_dir / "config" / "provider_endpoint_profiles.json").resolve()
        self.core_backed = _is_gateway_root(self.base_dir) if core_backed is None else bool(core_backed)
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
        if self.core_backed:
            return self._load_core_profiles()
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

    def _load_core_profiles(self) -> List[ProviderEndpointProfile]:
        from . import core_config

        profiles: List[ProviderEndpointProfile] = []
        for row in core_config.list_core_provider_profiles():
            try:
                profiles.append(_profile_from_core_row(row))
            except ProviderEndpointProfileError:
                continue
        profiles.sort(key=lambda p: (p.scope, p.display_name.lower(), p.id.lower()))
        return profiles

    def _save_core_profiles(self, profiles: List[ProviderEndpointProfile]) -> None:
        """Publish this scope's profiles to the Core store: upserts, then deletes.

        A whole-list save rather than a per-row one, because `upsert_profile`
        and `delete_profile` both hand this method the list they want the store
        to hold. Core preserves what it is not told about (its own three-way
        save merge), so an upsert cannot revert a row another writer added
        between this store's read and its write.
        """

        from . import core_config

        wanted = {profile.id.lower() for profile in profiles}
        for profile in profiles:
            try:
                core_config.save_core_provider_profile(
                    profile.id,
                    display_name=profile.display_name,
                    description=profile.description,
                    provider_family=profile.provider_family,
                    base_url=profile.base_url,
                    api_key=profile.api_key,
                    clear_api_key=not str(profile.api_key or "").strip(),
                    allowed_models=list(profile.allowed_models),
                    enabled=bool(profile.enabled),
                    scope=profile.scope,
                    capabilities=list(profile.capabilities),
                    created_at=profile.created_at or None,
                )
            except ValueError as exc:
                # ONE STORE, ONE VALIDATOR: the values Core accepts are the
                # values a profile may carry, and the reason travels verbatim
                # to the console instead of becoming a bare 500.
                raise ProviderEndpointProfileError(str(exc)) from exc
        for row in core_config.list_core_provider_profiles():
            profile_id = str(row.get("id") or "").strip()
            if profile_id and profile_id.lower() not in wanted:
                core_config.delete_core_provider_profile(profile_id)

    def _save_profiles_locked(self, profiles: List[ProviderEndpointProfile]) -> None:
        if self.core_backed:
            self._save_core_profiles(profiles)
            return
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
        # Unique temp per writer: the CLI and the server route write this store
        # concurrently, and a SHARED temp name lets one writer's open truncate
        # another's in-flight bytes and then keep writing into the inode the
        # first one already published (the AbstractCore config-store corruption
        # of 2026-08-01, same shape). Same directory keeps `replace` atomic.
        tmp = self.path.with_suffix(f".{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            tmp.replace(self.path)
        except BaseException:
            try:
                tmp.unlink()
            except Exception:
                pass
            raise
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass


def _profile_from_core_row(row: Dict[str, Any]) -> ProviderEndpointProfile:
    """One Core `provider_profiles` row, read as a Gateway profile.

    An `api_key_env_var` row resolves through the environment here exactly as
    it does in Core, so a profile stored as an env reference works from either
    console instead of silently arriving with no key.
    """

    api_key = str(row.get("api_key") or "")
    env_var = str(row.get("api_key_env_var") or "").strip()
    if not api_key.strip() and env_var:
        api_key = str(os.environ.get(env_var) or "")
    return ProviderEndpointProfile(
        id=normalize_profile_id(row.get("id")),
        display_name=normalize_display_name(row.get("display_name") or row.get("id")),
        description=normalize_description(row.get("description")),
        provider_family=normalize_provider_family(row.get("provider_family") or "openai-compatible"),
        base_url=normalize_base_url(row.get("base_url")),
        api_key=normalize_api_key(api_key),
        scope=normalize_scope(row.get("scope") or "gateway"),
        capabilities=normalize_string_list(row.get("capabilities"), default=_DEFAULT_CAPABILITIES),
        allowed_models=normalize_string_list(row.get("allowed_models"), default=()),
        enabled=bool(row.get("enabled", True)),
        created_at=normalize_timestamp(row.get("created_at")),
        updated_at=normalize_timestamp(row.get("updated_at")),
    )


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


def endpoint_profile_store_authority(*, base_dir: Path, root_base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """WHO OWNS THE FILE a shared profile is written to, in the same three keys
    the capability-defaults payload uses (`authority`, `config_file`,
    `writable`).

    A settings UI that lets an operator edit provider connections owes them the
    answer to "what am I actually editing" -- and since the one-store ruling
    (2026-08-01) the honest answer at the gateway root is AbstractCore's own
    store, not a Gateway file. A per-USER runtime still keeps a private file, so
    that path travels separately as `principal_config_file` rather than being
    passed off as the shared one.
    """

    root = Path(root_base_dir if root_base_dir is not None else base_dir).expanduser().resolve()
    current = Path(base_dir).expanduser().resolve()
    shared = ProviderEndpointProfileStore(base_dir=root)
    if shared.core_backed:
        from . import core_config

        path = str(core_config.core_config_file() or "")
        authority = "abstractcore.local"
    else:
        path = str(shared.path)
        authority = "abstractgateway.local"
    out: Dict[str, Any] = {
        "authority": authority,
        "config_file": path,
        "writable": _path_is_writable(path),
    }
    if current != root:
        out["principal_config_file"] = str(ProviderEndpointProfileStore(base_dir=current).path)
    return out


def _path_is_writable(path: Any) -> bool:
    """True when this process could save to `path` -- the file itself when it
    exists, else the directory that would receive it. A store nobody can write
    must say so instead of offering edit buttons that fail at save time."""

    text = str(path or "").strip()
    if not text:
        return False
    try:
        target = Path(text).expanduser()
        if target.exists():
            return os.access(target, os.W_OK)
        parent = target.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        return os.access(parent, os.W_OK)
    except Exception:
        return False


def explain_endpoint_profile_miss(provider: Any, *, base_dir: Path, root_base_dir: Optional[Path] = None) -> Optional[str]:
    """A plain-words reason why an endpoint profile did not resolve, when one
    is knowable — or None when the profile simply does not exist.

    The case that matters (release gap 2, delegate order c5863): a profile
    CREATED single-user carries the silent default scope 'user'; after
    multi-user auth turns on, per-principal services only inherit ROOT
    profiles scoped 'gateway', so the profile 'vanishes' with a misleading
    'not configured' error. Naming the real cause turns a mystery into a
    one-line fix an admin can apply."""
    profile_id = profile_id_from_virtual_provider(provider)
    if not profile_id:
        return None
    if root_base_dir is None or Path(root_base_dir).expanduser().resolve() == Path(base_dir).expanduser().resolve():
        return None
    for p in ProviderEndpointProfileStore(base_dir=root_base_dir).list_profiles():
        if p.id.lower() != profile_id.lower():
            continue
        if not p.enabled:
            return f"endpoint profile {profile_id!r} exists at the gateway root but is disabled — enable it to use it"
        if p.scope != "gateway":
            return (
                f"endpoint profile {profile_id!r} exists at the gateway root but its scope is {p.scope!r} "
                "(private to the admin's own runtime) — per-user runtimes only inherit profiles scoped "
                "'gateway'; an admin can share it by setting scope 'gateway' in the console's provider settings"
            )
    return None


def resolve_effective_endpoint_profile(provider: Any, *, base_dir: Path, root_base_dir: Optional[Path] = None) -> Optional[ProviderEndpointProfile]:
    profile_id = profile_id_from_virtual_provider(provider)
    if not profile_id:
        return None
    for profile in effective_endpoint_profiles(base_dir=base_dir, root_base_dir=root_base_dir):
        if profile.id.lower() == profile_id.lower() and profile.enabled:
            return profile
    return None
