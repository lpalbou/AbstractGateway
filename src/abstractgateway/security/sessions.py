from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Optional

from ..users import GatewayUserRegistry, gateway_data_dir_from_env, registry_credential_fingerprint, token_fingerprint
from .principal import GatewayPrincipal

_SESSION_VERSION = 1
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_EPHEMERAL_SESSION_SECRET = secrets.token_bytes(48)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_utc_iso() -> str:
    return _now().isoformat()


def _parse_dt(value: Any) -> Optional[datetime.datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(str(raw).strip())
    except Exception:
        return int(default)


def gateway_session_cookie_name() -> str:
    name = str(os.getenv("ABSTRACTGATEWAY_SESSION_COOKIE") or "abstractgateway_session").strip()
    return name or "abstractgateway_session"


def gateway_csrf_cookie_name() -> str:
    name = str(os.getenv("ABSTRACTGATEWAY_CSRF_COOKIE") or "abstractgateway_csrf").strip()
    return name or "abstractgateway_csrf"


def gateway_session_header_name() -> str:
    name = str(os.getenv("ABSTRACTGATEWAY_SESSION_HEADER") or "x-abstractgateway-session").strip().lower()
    return name or "x-abstractgateway-session"


def gateway_csrf_header_name() -> str:
    name = str(os.getenv("ABSTRACTGATEWAY_CSRF_HEADER") or "x-abstractgateway-csrf").strip().lower()
    return name or "x-abstractgateway-csrf"


def gateway_session_ttl_s() -> int:
    ttl = _as_int(os.getenv("ABSTRACTGATEWAY_SESSION_TTL_S"), 8 * 60 * 60)
    return max(60, min(7 * 24 * 60 * 60, ttl))


def gateway_session_store_path_from_env() -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_SESSIONS_FILE")
    if raw and str(raw).strip():
        return Path(str(raw)).expanduser().resolve()
    return gateway_data_dir_from_env() / "auth" / "sessions.json"


def _session_secret_path_from_env() -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_SESSION_SECRET_FILE")
    if raw and str(raw).strip():
        return Path(str(raw)).expanduser().resolve()
    return gateway_data_dir_from_env() / "auth" / "session_secret"


def _session_secret() -> bytes:
    env_secret = str(os.getenv("ABSTRACTGATEWAY_SESSION_SECRET") or "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    path = _session_secret_path_from_env()
    try:
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                return secret.encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        path.write_text(secret + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except Exception:
            pass
        return secret.encode("utf-8")
    except Exception:
        return _EPHEMERAL_SESSION_SECRET


def _sign(session_id: str) -> str:
    digest = hmac.new(_session_secret(), str(session_id).encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:48]


def _encode_cookie(session_id: str) -> str:
    return f"agws_{session_id}.{_sign(session_id)}"


def _decode_cookie(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw.startswith("agws_") or "." not in raw:
        return None
    sid, sig = raw[5:].rsplit(".", 1)
    if not sid or not sig:
        return None
    if not hmac.compare_digest(_sign(sid), sig):
        return None
    return sid


def gateway_session_id_from_value(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    decoded = _decode_cookie(raw)
    if decoded:
        return decoded
    # Accept raw ids for same-process tests and internal server-to-server clients.
    if "." not in raw and len(raw) >= 16:
        return raw
    return None


def gateway_session_id_from_cookie_header(cookie_header: Optional[str]) -> Optional[str]:
    if not cookie_header:
        return None
    try:
        cookie = SimpleCookie()
        cookie.load(str(cookie_header))
        morsel = cookie.get(gateway_session_cookie_name())
        if morsel is None:
            return None
        return gateway_session_id_from_value(str(morsel.value or ""))
    except Exception:
        return None


def csrf_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8", errors="ignore")).hexdigest()


def legacy_principal_still_valid(principal: GatewayPrincipal, *, legacy_token_fingerprints: tuple[str, ...]) -> bool:
    if principal.source != "legacy-token":
        return True
    fp = str(principal.token_fingerprint or "").strip()
    return bool(fp and fp in set(legacy_token_fingerprints or ()))


@dataclass(frozen=True)
class GatewaySessionRecord:
    session_id: str
    user_id: str
    tenant_id: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    runtime_id: str
    source: str
    token_fingerprint: str
    credential_fingerprint: str
    csrf_hash: str
    created_at: str
    expires_at: str
    last_seen_at: str

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "runtime_id": self.runtime_id,
            "source": self.source,
            "token_fingerprint": self.token_fingerprint,
            "credential_fingerprint": self.credential_fingerprint,
            "csrf_hash": self.csrf_hash,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_seen_at": self.last_seen_at,
        }

    def to_principal(self) -> GatewayPrincipal:
        return GatewayPrincipal(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            roles=self.roles,
            scopes=self.scopes,
            token_fingerprint=self.token_fingerprint,
            runtime_id=self.runtime_id,
            source=self.source,
        )


def _normalize_record(raw: dict[str, Any]) -> Optional[GatewaySessionRecord]:
    sid = str(raw.get("session_id") or "").strip()
    user_id = str(raw.get("user_id") or "").strip()
    if not sid or not user_id:
        return None

    def _tuple(value: Any) -> tuple[str, ...]:
        if isinstance(value, list):
            return tuple(str(item).strip() for item in value if str(item or "").strip())
        return tuple(str(value or "").split(",")) if str(value or "").strip() else ()

    return GatewaySessionRecord(
        session_id=sid,
        user_id=user_id,
        tenant_id=str(raw.get("tenant_id") or "default"),
        roles=_tuple(raw.get("roles")),
        scopes=_tuple(raw.get("scopes")),
        runtime_id=str(raw.get("runtime_id") or user_id),
        source=str(raw.get("source") or "local"),
        token_fingerprint=str(raw.get("token_fingerprint") or ""),
        credential_fingerprint=str(raw.get("credential_fingerprint") or ""),
        csrf_hash=str(raw.get("csrf_hash") or ""),
        created_at=str(raw.get("created_at") or _now_utc_iso()),
        expires_at=str(raw.get("expires_at") or _now_utc_iso()),
        last_seen_at=str(raw.get("last_seen_at") or _now_utc_iso()),
    )


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.expanduser().resolve()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[resolved] = lock
        return lock


class GatewaySessionStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = (path or gateway_session_store_path_from_env()).expanduser().resolve()
        self._lock = _lock_for(self.path)

    def _load_unlocked(self) -> dict[str, GatewaySessionRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        rows = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {}
        out: dict[str, GatewaySessionRecord] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            rec = _normalize_record(item)
            if rec is not None:
                out[rec.session_id] = rec
        return out

    def _save_unlocked(self, records: dict[str, GatewaySessionRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = _now()
        live = [
            rec
            for rec in records.values()
            if (_parse_dt(rec.expires_at) is None or (_parse_dt(rec.expires_at) or now) > now)
        ]
        live.sort(key=lambda rec: rec.last_seen_at, reverse=True)
        max_sessions = max(10, min(10000, _as_int(os.getenv("ABSTRACTGATEWAY_MAX_SESSIONS"), 1000)))
        payload = {
            "version": _SESSION_VERSION,
            "updated_at": _now_utc_iso(),
            "sessions": [rec.to_storage_dict() for rec in live[:max_sessions]],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    def create_session(
        self,
        principal: GatewayPrincipal,
        *,
        ttl_s: Optional[int] = None,
    ) -> tuple[str, str, GatewaySessionRecord]:
        now = _now()
        ttl = gateway_session_ttl_s() if ttl_s is None else max(60, min(90 * 24 * 60 * 60, int(ttl_s)))
        credential_fp = ""
        if principal.source == "user-registry":
            rec = GatewayUserRegistry().get_user(principal.user_id, tenant_id=principal.tenant_id)
            if rec is None or not rec.enabled:
                raise ValueError("Gateway user is disabled or unavailable")
            credential_fp = registry_credential_fingerprint(rec)
        sid = secrets.token_urlsafe(32)
        csrf_token = "agcsrf_" + secrets.token_urlsafe(32)
        record = GatewaySessionRecord(
            session_id=sid,
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            roles=tuple(principal.roles or ()),
            scopes=tuple(principal.scopes or ()),
            runtime_id=principal.runtime_id or principal.user_id,
            source=principal.source,
            token_fingerprint=principal.token_fingerprint,
            credential_fingerprint=credential_fp,
            csrf_hash=csrf_token_hash(csrf_token),
            created_at=now.isoformat(),
            expires_at=(now + datetime.timedelta(seconds=ttl)).isoformat(),
            last_seen_at=now.isoformat(),
        )
        with self._lock:
            records = self._load_unlocked()
            records[sid] = record
            self._save_unlocked(records)
        return _encode_cookie(sid), csrf_token, record

    def authenticate_session(
        self,
        session_id: str,
        *,
        legacy_token_fingerprints: tuple[str, ...] = (),
    ) -> Optional[GatewayPrincipal]:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        now = _now()
        with self._lock:
            records = self._load_unlocked()
            rec = records.get(sid)
            if rec is None:
                return None
            expires = _parse_dt(rec.expires_at)
            if expires is not None and expires <= now:
                records.pop(sid, None)
                self._save_unlocked(records)
                return None
            principal = rec.to_principal()
            if principal.source == "user-registry":
                user = GatewayUserRegistry().get_user(principal.user_id, tenant_id=principal.tenant_id)
                if user is None or not user.enabled:
                    records.pop(sid, None)
                    self._save_unlocked(records)
                    return None
                if rec.credential_fingerprint and rec.credential_fingerprint != registry_credential_fingerprint(user):
                    records.pop(sid, None)
                    self._save_unlocked(records)
                    return None
                principal = user.to_principal(token_fingerprint_value=rec.token_fingerprint)
            elif not legacy_principal_still_valid(principal, legacy_token_fingerprints=legacy_token_fingerprints):
                records.pop(sid, None)
                self._save_unlocked(records)
                return None
            records[sid] = GatewaySessionRecord(
                session_id=rec.session_id,
                user_id=principal.user_id,
                tenant_id=principal.tenant_id,
                roles=tuple(principal.roles or ()),
                scopes=tuple(principal.scopes or ()),
                runtime_id=principal.runtime_id or principal.user_id,
                source=principal.source,
                token_fingerprint=rec.token_fingerprint,
                credential_fingerprint=rec.credential_fingerprint,
                csrf_hash=rec.csrf_hash,
                created_at=rec.created_at,
                expires_at=rec.expires_at,
                last_seen_at=now.isoformat(),
            )
            self._save_unlocked(records)
            return principal

    def verify_csrf_token(self, session_id: str, csrf_token: str) -> bool:
        sid = gateway_session_id_from_value(session_id)
        token = str(csrf_token or "").strip()
        if not sid or not token:
            return False
        with self._lock:
            rec = self._load_unlocked().get(sid)
        if rec is None or not rec.csrf_hash:
            return False
        return hmac.compare_digest(rec.csrf_hash, csrf_token_hash(token))

    def delete_session(self, session_id: str) -> bool:
        sid = gateway_session_id_from_value(session_id)
        if not sid:
            return False
        with self._lock:
            records = self._load_unlocked()
            existed = sid in records
            records.pop(sid, None)
            self._save_unlocked(records)
            return existed


def legacy_token_fingerprints(tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(token_fingerprint(token) for token in tokens if str(token or "").strip())
