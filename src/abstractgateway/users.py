from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .security.principal import GatewayPrincipal, safe_principal_component

_REGISTRY_VERSION = 2
_PBKDF2_ITERATIONS = 260_000
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _as_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _split_csv(raw: Optional[str]) -> list[str]:
    if raw is None:
        return []
    out: list[str] = []
    for part in str(raw).split(","):
        value = part.strip()
        if value:
            out.append(value)
    return out


def _normalize_email(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 254 or "@" not in raw:
        raise ValueError("email must be empty or a valid email address")
    local, _, domain = raw.partition("@")
    if not local.strip() or "." not in domain.strip():
        raise ValueError("email must be empty or a valid email address")
    return raw


def gateway_data_dir_from_env() -> Path:
    raw = (
        os.getenv("ABSTRACTGATEWAY_DATA_DIR")
        or os.getenv("ABSTRACTFLOW_RUNTIME_DIR")
        or os.getenv("ABSTRACTFLOW_GATEWAY_DATA_DIR")
        or "./runtime"
    )
    return Path(str(raw)).expanduser().resolve()


def gateway_user_registry_path_from_env() -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_USERS_FILE")
    if raw and str(raw).strip():
        return Path(str(raw)).expanduser().resolve()
    return gateway_data_dir_from_env() / "auth" / "users.json"


def gateway_user_auth_enabled() -> bool:
    raw = (
        os.getenv("ABSTRACTGATEWAY_USER_AUTH")
        or os.getenv("ABSTRACTGATEWAY_MULTI_USER")
        or os.getenv("ABSTRACTFLOW_GATEWAY_USER_AUTH")
    )
    if raw is not None:
        return _as_bool(raw, False)
    mode = str(os.getenv("ABSTRACTGATEWAY_AUTH_MODE") or "").strip().lower()
    if mode in {"user", "users", "multi-user", "multi_user", "hosted"}:
        return True
    if mode in {"legacy", "single", "single-user", "single_user", "local"}:
        return False
    # The registry file being present means user auth is configured and ready,
    # not that the Gateway should silently switch into hosted/multi-user mode.
    return _as_bool(os.getenv("ABSTRACTGATEWAY_USER_AUTH_AUTO"), False) and gateway_user_registry_path_from_env().exists()


def token_fingerprint(token: str) -> str:
    digest = hashlib.sha256(str(token or "").encode("utf-8", errors="ignore")).hexdigest()
    return digest[:12]


def registry_credential_fingerprint(record: "GatewayUserRecord") -> str:
    digest = hashlib.sha256(str(getattr(record, "token_hash", "") or "").encode("utf-8", errors="ignore")).hexdigest()
    return digest[:12]


def generate_gateway_token() -> str:
    return "agw_" + secrets.token_urlsafe(32)


def hash_gateway_token(token: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(token or "").encode("utf-8", errors="ignore"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_gateway_token(token: str, token_hash: str) -> bool:
    try:
        scheme, iterations_raw, salt_hex, digest_hex = str(token_hash or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(token or "").encode("utf-8", errors="ignore"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


@dataclass(frozen=True)
class GatewayUserRecord:
    user_id: str
    tenant_id: str = "default"
    email: str = ""
    roles: tuple[str, ...] = ("user",)
    scopes: tuple[str, ...] = ()
    enabled: bool = True
    runtime_id: str = ""
    token_hash: str = ""
    token_fingerprint: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "enabled": bool(self.enabled),
            "runtime_id": self.runtime_id or self.user_id,
            "token_hash": self.token_hash,
            "token_fingerprint": self.token_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "enabled": bool(self.enabled),
            "runtime_id": self.runtime_id or self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_principal(self, *, token_fingerprint_value: str = "") -> GatewayPrincipal:
        return GatewayPrincipal(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            roles=tuple(self.roles),
            scopes=tuple(self.scopes),
            token_fingerprint=token_fingerprint_value,
            runtime_id=self.runtime_id or self.user_id,
            source="user-registry",
        )


@dataclass(frozen=True)
class GatewayRuntimeReservation:
    tenant_id: str
    runtime_id: str
    owner_key: str
    owner_user_id: str
    reason: str = "deleted-user"
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.tenant_id}:{self.runtime_id}"

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "runtime_id": self.runtime_id,
            "owner_key": self.owner_key,
            "owner_user_id": self.owner_user_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "runtime_id": self.runtime_id,
            "owner_key": self.owner_key,
            "owner_user_id": self.owner_user_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _normalize_record(raw: dict[str, Any]) -> GatewayUserRecord:
    user_id = safe_principal_component(raw.get("user_id"), default="")
    tenant_id = safe_principal_component(raw.get("tenant_id"), default="default")
    if not user_id:
        raise ValueError("user_id is required")

    def _normalize_str_list(value: Any) -> tuple[str, ...]:
        if isinstance(value, list):
            out: list[str] = []
            for item in value:
                s = str(item or "").strip()
                if s:
                    out.append(s)
            return tuple(out)
        return tuple(_split_csv(value))

    roles = _normalize_str_list(raw.get("roles"))
    scopes = _normalize_str_list(raw.get("scopes"))
    created_at = str(raw.get("created_at") or _now_utc_iso())
    updated_at = str(raw.get("updated_at") or created_at)
    return GatewayUserRecord(
        user_id=user_id,
        tenant_id=tenant_id,
        email=_normalize_email(raw.get("email")),
        roles=roles or ("user",),
        scopes=scopes,
        enabled=bool(raw.get("enabled", True)),
        runtime_id=safe_principal_component(raw.get("runtime_id") or user_id, default=user_id),
        token_hash=str(raw.get("token_hash") or ""),
        token_fingerprint=str(raw.get("token_fingerprint") or ""),
        created_at=created_at,
        updated_at=updated_at,
    )


def _runtime_reservation_key(*, tenant_id: str, runtime_id: str) -> str:
    return f"{safe_principal_component(tenant_id, default='default')}:{safe_principal_component(runtime_id, default='')}"


def _normalize_runtime_reservation(raw: dict[str, Any]) -> GatewayRuntimeReservation:
    tenant_id = safe_principal_component(raw.get("tenant_id"), default="default")
    runtime_id = safe_principal_component(raw.get("runtime_id"), default="")
    owner_user_id = safe_principal_component(raw.get("owner_user_id"), default="")
    owner_key = str(raw.get("owner_key") or "").strip()
    if not owner_key and owner_user_id:
        owner_key = f"{tenant_id}:{owner_user_id}"
    if not runtime_id or not owner_key:
        raise ValueError("runtime reservation requires runtime_id and owner_key")
    if not owner_user_id and ":" in owner_key:
        owner_user_id = safe_principal_component(owner_key.split(":", 1)[1], default="")
    created_at = str(raw.get("created_at") or _now_utc_iso())
    updated_at = str(raw.get("updated_at") or created_at)
    return GatewayRuntimeReservation(
        tenant_id=tenant_id,
        runtime_id=runtime_id,
        owner_key=owner_key,
        owner_user_id=owner_user_id,
        reason=str(raw.get("reason") or "deleted-user"),
        created_at=created_at,
        updated_at=updated_at,
    )


def _registry_lock(path: Path) -> threading.RLock:
    resolved = path.expanduser().resolve()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[resolved] = lock
        return lock


class GatewayUserRegistry:
    def __init__(self, path: Optional[Path] = None):
        self.path = (path or gateway_user_registry_path_from_env()).expanduser().resolve()
        self._lock = _registry_lock(self.path)

    def _load_store_unlocked(self) -> tuple[dict[str, GatewayUserRecord], dict[str, GatewayRuntimeReservation]]:
        if not self.path.exists():
            return {}, {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid Gateway user registry: {self.path}: {exc}") from exc
        records = data.get("users") if isinstance(data, dict) else None
        reservations = data.get("runtime_reservations") if isinstance(data, dict) else None
        reservation_out: dict[str, GatewayRuntimeReservation] = {}
        if isinstance(reservations, list):
            for item in reservations:
                if not isinstance(item, dict):
                    continue
                try:
                    reservation = _normalize_runtime_reservation(item)
                except ValueError:
                    continue
                reservation_out[reservation.key] = reservation
        if not isinstance(records, list):
            return {}, reservation_out
        out: dict[str, GatewayUserRecord] = {}
        for item in records:
            if not isinstance(item, dict):
                continue
            rec = _normalize_record(item)
            out[rec.key] = rec
        return out, reservation_out

    def _load_unlocked(self) -> dict[str, GatewayUserRecord]:
        records, _reservations = self._load_store_unlocked()
        return records

    def _save_store_unlocked(
        self,
        records: dict[str, GatewayUserRecord],
        reservations: dict[str, GatewayRuntimeReservation],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _REGISTRY_VERSION,
            "updated_at": _now_utc_iso(),
            "runtime_reservations": [
                rec.to_storage_dict() for rec in sorted(reservations.values(), key=lambda r: (r.tenant_id, r.runtime_id))
            ],
            "users": [rec.to_storage_dict() for rec in sorted(records.values(), key=lambda r: (r.tenant_id, r.user_id))],
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

    def _save_unlocked(self, records: dict[str, GatewayUserRecord]) -> None:
        _existing_records, reservations = self._load_store_unlocked()
        self._save_store_unlocked(records, reservations)

    def _require_runtime_available_unlocked(
        self,
        records: dict[str, GatewayUserRecord],
        reservations: dict[str, GatewayRuntimeReservation],
        *,
        tenant_id: str,
        runtime_id: str,
        owner_key: str,
    ) -> None:
        tenant0 = safe_principal_component(tenant_id, default="default")
        runtime0 = safe_principal_component(runtime_id, default="")
        if not runtime0:
            raise ValueError("runtime_id is required")
        for rec in records.values():
            if rec.key == owner_key:
                continue
            if rec.tenant_id == tenant0 and (rec.runtime_id or rec.user_id) == runtime0:
                raise ValueError(f"Gateway runtime already assigned in tenant {tenant0}: {runtime0}")
        reservation = reservations.get(_runtime_reservation_key(tenant_id=tenant0, runtime_id=runtime0))
        if reservation is not None and reservation.owner_key != owner_key:
            raise ValueError(
                f"Gateway runtime already assigned or retained in tenant {tenant0}: {runtime0}"
            )

    def _reserve_runtime_unlocked(
        self,
        reservations: dict[str, GatewayRuntimeReservation],
        *,
        record: GatewayUserRecord,
        reason: str,
    ) -> None:
        tenant_id = safe_principal_component(record.tenant_id, default="default")
        runtime_id = safe_principal_component(record.runtime_id or record.user_id, default=record.user_id)
        if not runtime_id:
            return
        now = _now_utc_iso()
        key = _runtime_reservation_key(tenant_id=tenant_id, runtime_id=runtime_id)
        existing = reservations.get(key)
        created_at = existing.created_at if existing is not None else now
        reservations[key] = GatewayRuntimeReservation(
            tenant_id=tenant_id,
            runtime_id=runtime_id,
            owner_key=record.key,
            owner_user_id=record.user_id,
            reason=reason,
            created_at=created_at,
            updated_at=now,
        )

    def list_users(self) -> list[GatewayUserRecord]:
        with self._lock:
            return sorted(self._load_unlocked().values(), key=lambda r: (r.tenant_id, r.user_id))

    def list_runtime_reservations(self) -> list[GatewayRuntimeReservation]:
        with self._lock:
            _records, reservations = self._load_store_unlocked()
            return sorted(reservations.values(), key=lambda r: (r.tenant_id, r.runtime_id))

    def get_runtime_reservation(self, *, tenant_id: str = "default", runtime_id: str) -> Optional[GatewayRuntimeReservation]:
        key = _runtime_reservation_key(tenant_id=tenant_id, runtime_id=runtime_id)
        with self._lock:
            _records, reservations = self._load_store_unlocked()
            return reservations.get(key)

    def get_user(self, user_id: str, *, tenant_id: str = "default") -> Optional[GatewayUserRecord]:
        key = f"{safe_principal_component(tenant_id, default='default')}:{safe_principal_component(user_id, default='')}"
        with self._lock:
            return self._load_unlocked().get(key)

    def create_user(
        self,
        *,
        user_id: str,
        tenant_id: str = "default",
        roles: Optional[list[str]] = None,
        scopes: Optional[list[str]] = None,
        enabled: bool = True,
        runtime_id: Optional[str] = None,
        email: Optional[str] = None,
        token: Optional[str] = None,
    ) -> tuple[GatewayUserRecord, str]:
        user_id0 = safe_principal_component(user_id, default="")
        tenant_id0 = safe_principal_component(tenant_id, default="default")
        if not user_id0:
            raise ValueError("user_id is required")
        issued_token = str(token or "").strip() or generate_gateway_token()
        issued_token_fp = token_fingerprint(issued_token)
        now = _now_utc_iso()
        rec = GatewayUserRecord(
            user_id=user_id0,
            tenant_id=tenant_id0,
            email=_normalize_email(email),
            roles=tuple(roles or ["user"]),
            scopes=tuple(scopes or []),
            enabled=bool(enabled),
            runtime_id=safe_principal_component(runtime_id or user_id0, default=user_id0),
            token_hash=hash_gateway_token(issued_token),
            token_fingerprint=issued_token_fp,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            records, reservations = self._load_store_unlocked()
            if rec.key in records:
                raise ValueError(f"Gateway user already exists: {tenant_id0}/{user_id0}")
            self._require_runtime_available_unlocked(
                records,
                reservations,
                tenant_id=rec.tenant_id,
                runtime_id=rec.runtime_id or rec.user_id,
                owner_key=rec.key,
            )
            records[rec.key] = rec
            reservations.pop(_runtime_reservation_key(tenant_id=rec.tenant_id, runtime_id=rec.runtime_id or rec.user_id), None)
            self._save_store_unlocked(records, reservations)
        return rec, issued_token

    def update_user(
        self,
        *,
        user_id: str,
        tenant_id: str = "default",
        roles: Optional[list[str]] = None,
        scopes: Optional[list[str]] = None,
        enabled: Optional[bool] = None,
        runtime_id: Optional[str] = None,
        email: Optional[str] = None,
        token: Optional[str] = None,
    ) -> tuple[GatewayUserRecord, Optional[str]]:
        key = f"{safe_principal_component(tenant_id, default='default')}:{safe_principal_component(user_id, default='')}"
        issued_token: Optional[str] = None
        with self._lock:
            records, reservations = self._load_store_unlocked()
            rec = records.get(key)
            if rec is None:
                raise KeyError(f"Gateway user not found: {tenant_id}/{user_id}")
            token_hash = rec.token_hash
            token_fp = rec.token_fingerprint
            if token is not None:
                issued_token = str(token or "").strip() or generate_gateway_token()
                token_hash = hash_gateway_token(issued_token)
                token_fp = token_fingerprint(issued_token)
            updated = GatewayUserRecord(
                user_id=rec.user_id,
                tenant_id=rec.tenant_id,
                email=_normalize_email(email) if email is not None else rec.email,
                roles=tuple(roles) if roles is not None else rec.roles,
                scopes=tuple(scopes) if scopes is not None else rec.scopes,
                enabled=bool(enabled) if enabled is not None else rec.enabled,
                runtime_id=safe_principal_component(runtime_id or rec.runtime_id, default=rec.user_id),
                token_hash=token_hash,
                token_fingerprint=token_fp,
                created_at=rec.created_at,
                updated_at=_now_utc_iso(),
            )
            self._require_runtime_available_unlocked(
                records,
                reservations,
                tenant_id=updated.tenant_id,
                runtime_id=updated.runtime_id or updated.user_id,
                owner_key=updated.key,
            )
            if (rec.runtime_id or rec.user_id) != (updated.runtime_id or updated.user_id):
                self._reserve_runtime_unlocked(reservations, record=rec, reason="runtime-reassigned")
            reservations.pop(_runtime_reservation_key(tenant_id=updated.tenant_id, runtime_id=updated.runtime_id or updated.user_id), None)
            records[key] = updated
            self._save_store_unlocked(records, reservations)
            return updated, issued_token

    def delete_user(self, *, user_id: str, tenant_id: str = "default") -> bool:
        key = f"{safe_principal_component(tenant_id, default='default')}:{safe_principal_component(user_id, default='')}"
        with self._lock:
            records, reservations = self._load_store_unlocked()
            rec = records.get(key)
            if rec is None:
                return False
            records.pop(key, None)
            self._reserve_runtime_unlocked(reservations, record=rec, reason="deleted-user")
            self._save_store_unlocked(records, reservations)
            return True

    def release_runtime_reservation(self, *, tenant_id: str = "default", runtime_id: str) -> bool:
        tenant0 = safe_principal_component(tenant_id, default="default")
        runtime0 = safe_principal_component(runtime_id, default="")
        if not runtime0:
            raise ValueError("runtime_id is required")
        key = _runtime_reservation_key(tenant_id=tenant0, runtime_id=runtime0)
        with self._lock:
            records, reservations = self._load_store_unlocked()
            if key not in reservations:
                return False
            for rec in records.values():
                if rec.tenant_id == tenant0 and (rec.runtime_id or rec.user_id) == runtime0:
                    raise ValueError(f"Gateway runtime is assigned to active user {rec.tenant_id}/{rec.user_id}")
            reservations.pop(key, None)
            self._save_store_unlocked(records, reservations)
            return True

    def mark_runtime_reservation_purging(
        self,
        *,
        tenant_id: str = "default",
        runtime_id: str,
    ) -> GatewayRuntimeReservation:
        tenant0 = safe_principal_component(tenant_id, default="default")
        runtime0 = safe_principal_component(runtime_id, default="")
        if not runtime0:
            raise ValueError("runtime_id is required")
        key = _runtime_reservation_key(tenant_id=tenant0, runtime_id=runtime0)
        with self._lock:
            records, reservations = self._load_store_unlocked()
            reservation = reservations.get(key)
            if reservation is None:
                raise KeyError(f"Gateway runtime reservation not found: {tenant0}/{runtime0}")
            for rec in records.values():
                if rec.tenant_id == tenant0 and (rec.runtime_id or rec.user_id) == runtime0:
                    raise ValueError(f"Gateway runtime is assigned to active user {rec.tenant_id}/{rec.user_id}")
            now = _now_utc_iso()
            purging = GatewayRuntimeReservation(
                tenant_id=reservation.tenant_id,
                runtime_id=reservation.runtime_id,
                owner_key=reservation.owner_key,
                owner_user_id=reservation.owner_user_id,
                reason="purging",
                created_at=reservation.created_at,
                updated_at=now,
            )
            reservations[key] = purging
            self._save_store_unlocked(records, reservations)
            return purging

    def transfer_runtime_reservation(
        self,
        *,
        tenant_id: str = "default",
        runtime_id: str,
        target_user_id: str,
    ) -> tuple[GatewayUserRecord, GatewayRuntimeReservation, str]:
        tenant0 = safe_principal_component(tenant_id, default="default")
        runtime0 = safe_principal_component(runtime_id, default="")
        target0 = safe_principal_component(target_user_id, default="")
        if not runtime0:
            raise ValueError("runtime_id is required")
        if not target0:
            raise ValueError("target_user_id is required")
        reservation_key = _runtime_reservation_key(tenant_id=tenant0, runtime_id=runtime0)
        target_key = f"{tenant0}:{target0}"
        with self._lock:
            records, reservations = self._load_store_unlocked()
            reservation = reservations.get(reservation_key)
            if reservation is None:
                raise KeyError(f"Gateway runtime reservation not found: {tenant0}/{runtime0}")
            if reservation.reason == "purging":
                raise ValueError(f"Gateway runtime purge is already in progress: {tenant0}/{runtime0}")
            target = records.get(target_key)
            if target is None:
                raise KeyError(f"Gateway user not found: {tenant0}/{target0}")
            for rec in records.values():
                if rec.key == target.key:
                    continue
                if rec.tenant_id == tenant0 and (rec.runtime_id or rec.user_id) == runtime0:
                    raise ValueError(f"Gateway runtime already assigned in tenant {tenant0}: {runtime0}")
            previous_runtime_id = safe_principal_component(target.runtime_id or target.user_id, default=target.user_id)
            updated = GatewayUserRecord(
                user_id=target.user_id,
                tenant_id=target.tenant_id,
                email=target.email,
                roles=target.roles,
                scopes=target.scopes,
                enabled=target.enabled,
                runtime_id=runtime0,
                token_hash=target.token_hash,
                token_fingerprint=target.token_fingerprint,
                created_at=target.created_at,
                updated_at=_now_utc_iso(),
            )
            if previous_runtime_id != runtime0:
                self._reserve_runtime_unlocked(reservations, record=target, reason="runtime-transferred")
            records[target.key] = updated
            reservations.pop(reservation_key, None)
            self._save_store_unlocked(records, reservations)
            return updated, reservation, previous_runtime_id

    def authenticate(self, token: str) -> Optional[GatewayPrincipal]:
        if not token:
            return None
        with self._lock:
            records = self._load_unlocked()
        fp = token_fingerprint(token)
        for rec in records.values():
            if not rec.enabled or not rec.token_hash:
                continue
            if verify_gateway_token(token, rec.token_hash):
                return rec.to_principal(token_fingerprint_value=fp)
        return None
