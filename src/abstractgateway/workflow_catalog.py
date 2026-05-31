from __future__ import annotations

import datetime
import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from abstractruntime.workflow_bundle import open_workflow_bundle
from abstractruntime.workflow_bundle.registry import sanitize_bundle_id, sanitize_bundle_version

from .security.principal import GatewayPrincipal, safe_principal_component
from .users import gateway_data_dir_from_env


CATALOG_VERSION = 1
CATALOG_SCOPE_TENANT = "tenant_catalog"
CATALOG_SCOPE_FRAMEWORK = "framework_catalog"
CATALOG_SCOPES = {CATALOG_SCOPE_TENANT, CATALOG_SCOPE_FRAMEWORK}
STARTABLE_STATUSES = {"published"}
INACTIVE_STATUSES = {"deprecated", "blocked", "tombstoned"}
CATALOG_INTERNAL_PREFIX = "__catalog__"
CATALOG_INTERNAL_V2_PREFIX = f"{CATALOG_INTERNAL_PREFIX}v2__"
WORKFLOW_POLICY_SIGNATURE_FIELD = "signature"

_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class WorkflowCatalogError(RuntimeError):
    pass


class WorkflowCatalogNotFoundError(WorkflowCatalogError):
    pass


class WorkflowCatalogForbiddenError(WorkflowCatalogError):
    pass


class WorkflowCatalogImmutableError(WorkflowCatalogError):
    pass


class WorkflowCatalogStatusError(WorkflowCatalogError):
    pass


@dataclass(frozen=True)
class WorkflowCatalogStartSelection:
    scope: str
    tenant_id: str
    bundle_id: str
    bundle_version: str
    flow_id: str
    sha256: str
    status: str
    host_bundle_id: str

    @property
    def host_workflow_id(self) -> str:
        return f"{self.host_bundle_id}@{self.bundle_version}:{self.flow_id}"

    def policy_snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "registry_scope": self.scope,
            "tenant_id": self.tenant_id,
            "bundle_id": self.bundle_id,
            "bundle_version": self.bundle_version,
            "flow_id": self.flow_id,
            "sha256": self.sha256,
            "status": self.status,
            "run_as": "caller",
            "host_workflow_id": self.host_workflow_id,
        }


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(bytes(data or b"")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.expanduser().resolve()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[resolved] = lock
        return lock


def normalize_catalog_scope(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in {"", "catalog", "tenant"}:
        return CATALOG_SCOPE_TENANT
    if raw in {"framework", "global"}:
        return CATALOG_SCOPE_FRAMEWORK
    if raw not in CATALOG_SCOPES:
        raise ValueError(f"Unsupported workflow catalog scope: {value}")
    return raw


def catalog_key(scope: str, tenant_id: str) -> str:
    scope0 = normalize_catalog_scope(scope)
    tenant0 = safe_principal_component(tenant_id, default="default")
    return f"{scope0}:{tenant0}"


def catalog_internal_bundle_id(*, tenant_id: str, bundle_id: str, scope: str = CATALOG_SCOPE_TENANT) -> str:
    scope0 = normalize_catalog_scope(scope)
    tenant0 = safe_principal_component(tenant_id, default="default")
    bundle0 = str(bundle_id or "").strip()
    if not bundle0:
        raise ValueError("bundle_id is required")
    return f"{CATALOG_INTERNAL_V2_PREFIX}{scope0}__{_b64_component(tenant0)}__{_b64_component(bundle0)}"


def _b64_component(value: str) -> str:
    raw = str(value or "").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64_component(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        padded = raw + ("=" * (-len(raw) % 4))
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def parse_catalog_internal_bundle_id(bundle_id: str) -> Optional[tuple[str, str, str]]:
    s = str(bundle_id or "").strip()
    if not s.startswith(CATALOG_INTERNAL_PREFIX):
        return None
    if s.startswith(CATALOG_INTERNAL_V2_PREFIX):
        rest2 = s[len(CATALOG_INTERNAL_V2_PREFIX) :]
        parts2 = rest2.split("__", 2)
        if len(parts2) != 3:
            return None
        scope2, tenant_enc, bundle_enc = parts2
        tenant_id = _unb64_component(tenant_enc)
        public_bundle_id = _unb64_component(bundle_enc)
        if not scope2 or not tenant_id or not public_bundle_id:
            return None
        try:
            scope2 = normalize_catalog_scope(scope2)
        except Exception:
            return None
        return (scope2, tenant_id, public_bundle_id)
    rest = s[len(CATALOG_INTERNAL_PREFIX) :]
    parts = rest.split("__", 2)
    if len(parts) != 3:
        return None
    scope, tenant_id, public_bundle_id = parts
    if not scope or not tenant_id or not public_bundle_id:
        return None
    try:
        scope = normalize_catalog_scope(scope)
    except Exception:
        return None
    return (scope, tenant_id, public_bundle_id)


def workflow_catalog_path_from_env(root_data_dir: Optional[Path] = None) -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_WORKFLOW_CATALOG_FILE")
    if raw and str(raw).strip():
        return Path(str(raw)).expanduser().resolve()
    root = Path(root_data_dir).expanduser().resolve() if root_data_dir is not None else gateway_data_dir_from_env()
    return root / "workflow_catalog.json"


def workflow_catalog_bundles_root_from_env(root_data_dir: Optional[Path] = None) -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_WORKFLOW_CATALOG_BUNDLES_DIR")
    if raw and str(raw).strip():
        return Path(str(raw)).expanduser().resolve()
    root = Path(root_data_dir).expanduser().resolve() if root_data_dir is not None else gateway_data_dir_from_env()
    return root / "workflow_catalog" / "bundles"


def workflow_policy_secret_path_from_env(root_data_dir: Optional[Path] = None) -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_WORKFLOW_POLICY_SECRET_FILE")
    if raw and str(raw).strip():
        return Path(str(raw)).expanduser().resolve()
    root = Path(root_data_dir).expanduser().resolve() if root_data_dir is not None else gateway_data_dir_from_env()
    return root / ".workflow_policy_secret"


def load_or_create_workflow_policy_secret(root_data_dir: Optional[Path] = None) -> str:
    raw = os.getenv("ABSTRACTGATEWAY_WORKFLOW_POLICY_SECRET")
    if raw and str(raw).strip():
        return str(raw).strip()
    path = workflow_policy_secret_path_from_env(root_data_dir)
    lock = _lock_for(path)
    with lock:
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
        except Exception:
            pass
        secret = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secret + "\n")
        except FileExistsError:
            existing = path.read_text(encoding="utf-8").strip()
            return existing or secret
        return secret


def _workflow_policy_signing_payload(policy: dict[str, Any]) -> bytes:
    payload = dict(policy or {})
    payload.pop(WORKFLOW_POLICY_SIGNATURE_FIELD, None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_workflow_policy(policy: dict[str, Any], *, secret: str) -> dict[str, Any]:
    out = dict(policy or {})
    sig = hmac.new(str(secret or "").encode("utf-8"), _workflow_policy_signing_payload(out), hashlib.sha256).hexdigest()
    out[WORKFLOW_POLICY_SIGNATURE_FIELD] = f"v1:{sig}"
    return out


def verify_workflow_policy_signature(policy: Any, *, secret: str) -> bool:
    if not isinstance(policy, dict):
        return False
    raw_sig = policy.get(WORKFLOW_POLICY_SIGNATURE_FIELD)
    if not isinstance(raw_sig, str) or not raw_sig.startswith("v1:"):
        return False
    expected = hmac.new(str(secret or "").encode("utf-8"), _workflow_policy_signing_payload(policy), hashlib.sha256).hexdigest()
    return hmac.compare_digest(raw_sig, f"v1:{expected}")


def _normalize_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else str(value).split(",")
    out: list[str] = []
    for item in raw_items:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def normalize_catalog_acl(value: Any) -> dict[str, list[str]]:
    raw = value if isinstance(value, dict) else {}
    roles = _normalize_str_list(raw.get("roles"))
    users = _normalize_str_list(raw.get("users"))
    deny_users = _normalize_str_list(raw.get("deny_users") or raw.get("denied_users"))
    if not roles and not users:
        roles = ["user", "admin"]
    return {"roles": roles, "users": users, "deny_users": deny_users}


def _principal_user_keys(principal: GatewayPrincipal) -> set[str]:
    tenant = safe_principal_component(principal.tenant_id, default="default")
    user = str(principal.user_id or "").strip()
    keys = {user} if user else set()
    if user:
        keys.add(f"{tenant}:{user}")
    return keys


def principal_can_run_catalog_record(record: dict[str, Any], principal: GatewayPrincipal) -> bool:
    if principal.is_admin():
        return True
    tenant_id = safe_principal_component(record.get("tenant_id"), default="default")
    if safe_principal_component(principal.tenant_id, default="default") != tenant_id:
        return False
    acl = normalize_catalog_acl(record.get("acl"))
    user_keys = _principal_user_keys(principal)
    if user_keys.intersection(set(acl.get("deny_users") or [])):
        return False
    allowed_users = set(acl.get("users") or [])
    if allowed_users:
        return bool(user_keys.intersection(allowed_users))
    roles = {str(r).strip() for r in tuple(principal.roles or ()) if str(r).strip()}
    allowed_roles = {str(r).strip() for r in list(acl.get("roles") or []) if str(r).strip()}
    return bool(roles.intersection(allowed_roles))


def catalog_record_public_dict(record: dict[str, Any], principal: Optional[GatewayPrincipal] = None) -> dict[str, Any]:
    status = str(record.get("status") or "published").strip().lower() or "published"
    can_run = False
    if principal is not None:
        can_run = status in STARTABLE_STATUSES and principal_can_run_catalog_record(record, principal)
    out = {
        "scope": str(record.get("scope") or CATALOG_SCOPE_TENANT),
        "tenant_id": str(record.get("tenant_id") or "default"),
        "bundle_id": str(record.get("bundle_id") or ""),
        "bundle_version": str(record.get("bundle_version") or ""),
        "bundle_ref": f"{record.get('bundle_id')}@{record.get('bundle_version')}",
        "sha256": str(record.get("sha256") or ""),
        "status": status,
        "visibility": str(record.get("visibility") or "tenant"),
        "acl": normalize_catalog_acl(record.get("acl")),
        "run_policy": dict(record.get("run_policy") if isinstance(record.get("run_policy"), dict) else {"run_as": "caller"}),
        "default_entrypoint": str(record.get("default_entrypoint") or "") or None,
        "entrypoints": list(record.get("entrypoints") if isinstance(record.get("entrypoints"), list) else []),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "published_at": str(record.get("published_at") or ""),
        "status_reason": str(record.get("status_reason") or "") or None,
        "is_default": bool(record.get("is_default")),
        "default_version": str(record.get("default_version") or "") or None,
        "actions": {
            "can_run": bool(can_run),
            "can_inspect": bool(can_run or (principal is not None and principal.is_admin())),
            "can_admin": bool(principal is not None and principal.is_admin()),
        },
    }
    return out


class WorkflowCatalogStore:
    def __init__(self, *, root_data_dir: Optional[Path] = None, path: Optional[Path] = None):
        root = Path(root_data_dir).expanduser().resolve() if root_data_dir is not None else gateway_data_dir_from_env()
        self.root_data_dir = root
        self.path = (path or workflow_catalog_path_from_env(root)).expanduser().resolve()
        self.bundles_root = workflow_catalog_bundles_root_from_env(root)
        self._lock = _lock_for(self.path)

    def bundle_dir(self, *, scope: str, tenant_id: str) -> Path:
        scope0 = normalize_catalog_scope(scope)
        tenant0 = safe_principal_component(tenant_id, default="default")
        return (self.bundles_root / scope0 / tenant0).expanduser().resolve()

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": CATALOG_VERSION, "catalogs": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid workflow catalog: {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            return {"version": CATALOG_VERSION, "catalogs": {}}
        data.setdefault("version", CATALOG_VERSION)
        if not isinstance(data.get("catalogs"), dict):
            data["catalogs"] = {}
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def _catalog_unlocked(self, data: dict[str, Any], *, scope: str, tenant_id: str) -> dict[str, Any]:
        catalogs = data.setdefault("catalogs", {})
        key = catalog_key(scope, tenant_id)
        catalog = catalogs.setdefault(key, {"scope": normalize_catalog_scope(scope), "tenant_id": safe_principal_component(tenant_id, default="default"), "bundles": {}})
        if not isinstance(catalog.get("bundles"), dict):
            catalog["bundles"] = {}
        return catalog

    def get_record(self, *, scope: str = CATALOG_SCOPE_TENANT, tenant_id: str = "default", bundle_id: str, bundle_version: str) -> Optional[dict[str, Any]]:
        bid = str(bundle_id or "").strip()
        bver = str(bundle_version or "").strip()
        if not bid or not bver:
            return None
        with self._lock:
            data = self._load_unlocked()
            catalog = (data.get("catalogs") or {}).get(catalog_key(scope, tenant_id))
            bundles = catalog.get("bundles") if isinstance(catalog, dict) else None
            bundle = bundles.get(bid) if isinstance(bundles, dict) else None
            versions = bundle.get("versions") if isinstance(bundle, dict) else None
            record = versions.get(bver) if isinstance(versions, dict) else None
            return dict(record) if isinstance(record, dict) else None

    def get_default_record(self, *, scope: str = CATALOG_SCOPE_TENANT, tenant_id: str = "default", bundle_id: str) -> Optional[dict[str, Any]]:
        bid = str(bundle_id or "").strip()
        if not bid:
            return None
        with self._lock:
            data = self._load_unlocked()
            catalog = (data.get("catalogs") or {}).get(catalog_key(scope, tenant_id))
            bundles = catalog.get("bundles") if isinstance(catalog, dict) else None
            bundle = bundles.get(bid) if isinstance(bundles, dict) else None
            if not isinstance(bundle, dict):
                return None
            default_version = str(bundle.get("default_version") or "").strip()
            versions = bundle.get("versions") if isinstance(bundle.get("versions"), dict) else {}
            record = versions.get(default_version) if default_version else None
            return dict(record) if isinstance(record, dict) else None

    def list_records(
        self,
        *,
        principal: Optional[GatewayPrincipal] = None,
        scope: str = CATALOG_SCOPE_TENANT,
        tenant_id: str = "default",
        include_inactive: bool = False,
        include_denied: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load_unlocked()
            catalog = (data.get("catalogs") or {}).get(catalog_key(scope, tenant_id))
            bundles = catalog.get("bundles") if isinstance(catalog, dict) else None
            records: list[dict[str, Any]] = []
            if not isinstance(bundles, dict):
                return records
            for bundle in bundles.values():
                if not isinstance(bundle, dict):
                    continue
                default_version = str(bundle.get("default_version") or "").strip()
                versions = bundle.get("versions")
                if not isinstance(versions, dict):
                    continue
                for rec0 in versions.values():
                    if not isinstance(rec0, dict):
                        continue
                    rec = dict(rec0)
                    status = str(rec.get("status") or "published").strip().lower()
                    if not include_inactive and status not in STARTABLE_STATUSES:
                        continue
                    if principal is not None and not principal.is_admin() and not include_denied:
                        if not principal_can_run_catalog_record(rec, principal):
                            continue
                    rec["is_default"] = bool(default_version and str(rec.get("bundle_version") or "") == default_version)
                    rec["default_version"] = default_version or None
                    records.append(rec)
            records.sort(key=lambda r: (str(r.get("bundle_id") or ""), str(r.get("bundle_version") or "")))
            return records

    def install_bundle_bytes(
        self,
        content: bytes,
        *,
        scope: str = CATALOG_SCOPE_TENANT,
        tenant_id: str = "default",
        acl: Optional[dict[str, Any]] = None,
        make_default: bool = True,
        publisher: Optional[str] = None,
    ) -> dict[str, Any]:
        data_bytes = bytes(content or b"")
        if not data_bytes:
            raise ValueError("Bundle content is empty")

        scope0 = normalize_catalog_scope(scope)
        tenant0 = safe_principal_component(tenant_id, default="default")
        sha = _sha256_bytes(data_bytes)
        bundle_dir = self.bundle_dir(scope=scope0, tenant_id=tenant0)
        bundle_dir.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(prefix=".catalog_upload_", suffix=".flow", dir=str(bundle_dir))
        tmp: Optional[Path] = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data_bytes)
            bundle = open_workflow_bundle(tmp)
            man = bundle.manifest
            bid = str(getattr(man, "bundle_id", "") or "").strip()
            bver = str(getattr(man, "bundle_version", "") or "0.0.0").strip() or "0.0.0"
            if not bid:
                raise ValueError("Uploaded bundle has empty manifest.bundle_id")
            if sanitize_bundle_id(bid) != bid:
                raise ValueError(f"Bundle id '{bid}' contains unsafe characters")
            if sanitize_bundle_version(bver) != bver:
                raise ValueError(f"Bundle version '{bver}' contains unsafe characters")

            dest = (bundle_dir / f"{bid}@{bver}.flow").resolve()

            entrypoints: list[dict[str, Any]] = []
            for ep in list(getattr(man, "entrypoints", None) or []):
                fid = str(getattr(ep, "flow_id", "") or "").strip()
                if not fid:
                    continue
                entrypoints.append(
                    {
                        "flow_id": fid,
                        "name": getattr(ep, "name", None),
                        "description": str(getattr(ep, "description", "") or ""),
                        "interfaces": list(getattr(ep, "interfaces", None) or []),
                        "workflow_id": f"{bid}@{bver}:{fid}",
                    }
                )

            now = _now_utc_iso()
            with self._lock:
                raw = self._load_unlocked()
                catalog = self._catalog_unlocked(raw, scope=scope0, tenant_id=tenant0)
                bundles = catalog.setdefault("bundles", {})
                bundle_meta = bundles.setdefault(bid, {"default_version": None, "versions": {}})
                versions = bundle_meta.setdefault("versions", {})
                existing = versions.get(bver) if isinstance(versions, dict) else None
                if isinstance(existing, dict) and str(existing.get("sha256") or "") and str(existing.get("sha256")) != sha:
                    raise WorkflowCatalogImmutableError(
                        f"Catalog metadata for '{bid}@{bver}' already exists with a different sha256"
                    )
                if dest.exists():
                    existing_sha = _sha256_file(dest)
                    if existing_sha != sha:
                        raise WorkflowCatalogImmutableError(
                            f"Catalog bundle '{bid}@{bver}' already exists with different content"
                        )
                else:
                    try:
                        fd2 = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                        with os.fdopen(fd2, "wb") as out_f:
                            out_f.write(data_bytes)
                    except FileExistsError:
                        existing_sha = _sha256_file(dest)
                        if existing_sha != sha:
                            raise WorkflowCatalogImmutableError(
                                f"Catalog bundle '{bid}@{bver}' already exists with different content"
                            )
                    try:
                        if tmp and tmp.exists():
                            tmp.unlink()
                    except Exception:
                        pass
                    tmp = None
                created_at = str(existing.get("created_at") or now) if isinstance(existing, dict) else now
                status = str(existing.get("status") or "published") if isinstance(existing, dict) else "published"
                status = status.strip().lower() or "published"
                record = {
                    "scope": scope0,
                    "tenant_id": tenant0,
                    "bundle_id": bid,
                    "bundle_version": bver,
                    "sha256": sha,
                    "status": status,
                    "visibility": "tenant" if scope0 == CATALOG_SCOPE_TENANT else "framework",
                    "acl": normalize_catalog_acl(acl if acl is not None else (existing.get("acl") if isinstance(existing, dict) else None)),
                    "run_policy": {"run_as": "caller"},
                    "entrypoints": entrypoints,
                    "default_entrypoint": str(getattr(man, "default_entrypoint", "") or "") or None,
                    "created_at": created_at,
                    "updated_at": now,
                    "published_at": str(existing.get("published_at") or now) if isinstance(existing, dict) else now,
                    "publisher": str(publisher or (existing.get("publisher") if isinstance(existing, dict) else "") or ""),
                    "path": str(dest),
                }
                versions[bver] = record
                if status in STARTABLE_STATUSES and (make_default or not str(bundle_meta.get("default_version") or "").strip()):
                    bundle_meta["default_version"] = bver
                    bundle_meta["default_updated_at"] = now
                self._save_unlocked(raw)
                record["is_default"] = str(bundle_meta.get("default_version") or "") == bver
                record["default_version"] = str(bundle_meta.get("default_version") or "") or None
                return dict(record)
        finally:
            try:
                if tmp and tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def set_default(
        self,
        *,
        scope: str = CATALOG_SCOPE_TENANT,
        tenant_id: str = "default",
        bundle_id: str,
        bundle_version: str,
        updated_by: str = "",
    ) -> dict[str, Any]:
        scope0 = normalize_catalog_scope(scope)
        tenant0 = safe_principal_component(tenant_id, default="default")
        bid = str(bundle_id or "").strip()
        bver = str(bundle_version or "").strip()
        if not bid or not bver:
            raise ValueError("bundle_id and bundle_version are required")
        with self._lock:
            data = self._load_unlocked()
            catalog = self._catalog_unlocked(data, scope=scope0, tenant_id=tenant0)
            bundle = (catalog.get("bundles") or {}).get(bid)
            versions = bundle.get("versions") if isinstance(bundle, dict) else None
            rec = versions.get(bver) if isinstance(versions, dict) else None
            if not isinstance(rec, dict):
                raise WorkflowCatalogNotFoundError(f"Catalog bundle '{bid}@{bver}' not found")
            status = str(rec.get("status") or "published").strip().lower()
            if status not in STARTABLE_STATUSES:
                raise WorkflowCatalogStatusError(f"Cannot make non-published catalog bundle '{bid}@{bver}' the default")
            now = _now_utc_iso()
            bundle["default_version"] = bver
            bundle["default_updated_at"] = now
            bundle["default_updated_by"] = str(updated_by or "")
            self._save_unlocked(data)
            out = dict(rec)
            out["is_default"] = True
            out["default_version"] = bver
            return out

    def set_status(
        self,
        *,
        scope: str = CATALOG_SCOPE_TENANT,
        tenant_id: str = "default",
        bundle_id: str,
        bundle_version: str,
        status: str,
        reason: Optional[str] = None,
        updated_by: str = "",
    ) -> dict[str, Any]:
        scope0 = normalize_catalog_scope(scope)
        tenant0 = safe_principal_component(tenant_id, default="default")
        bid = str(bundle_id or "").strip()
        bver = str(bundle_version or "").strip()
        status0 = str(status or "").strip().lower()
        if status0 not in STARTABLE_STATUSES.union(INACTIVE_STATUSES):
            raise ValueError(f"Unsupported catalog status: {status}")
        with self._lock:
            data = self._load_unlocked()
            catalog = self._catalog_unlocked(data, scope=scope0, tenant_id=tenant0)
            bundle = (catalog.get("bundles") or {}).get(bid)
            versions = bundle.get("versions") if isinstance(bundle, dict) else None
            rec = versions.get(bver) if isinstance(versions, dict) else None
            if not isinstance(rec, dict):
                raise WorkflowCatalogNotFoundError(f"Catalog bundle '{bid}@{bver}' not found")
            now = _now_utc_iso()
            rec["status"] = status0
            rec["status_reason"] = str(reason or "").strip()
            rec["status_updated_at"] = now
            rec["status_updated_by"] = str(updated_by or "")
            rec["updated_at"] = now
            if status0 != "published" and isinstance(bundle, dict) and str(bundle.get("default_version") or "") == bver:
                bundle["default_version"] = None
                bundle["default_updated_at"] = now
                bundle["default_updated_by"] = str(updated_by or "")
            self._save_unlocked(data)
            return dict(rec)

    def set_acl(
        self,
        *,
        scope: str = CATALOG_SCOPE_TENANT,
        tenant_id: str = "default",
        bundle_id: str,
        bundle_version: str,
        acl: dict[str, Any],
        updated_by: str = "",
    ) -> dict[str, Any]:
        scope0 = normalize_catalog_scope(scope)
        tenant0 = safe_principal_component(tenant_id, default="default")
        bid = str(bundle_id or "").strip()
        bver = str(bundle_version or "").strip()
        with self._lock:
            data = self._load_unlocked()
            catalog = self._catalog_unlocked(data, scope=scope0, tenant_id=tenant0)
            bundle = (catalog.get("bundles") or {}).get(bid)
            versions = bundle.get("versions") if isinstance(bundle, dict) else None
            rec = versions.get(bver) if isinstance(versions, dict) else None
            if not isinstance(rec, dict):
                raise WorkflowCatalogNotFoundError(f"Catalog bundle '{bid}@{bver}' not found")
            rec["acl"] = normalize_catalog_acl(acl)
            rec["acl_updated_at"] = _now_utc_iso()
            rec["acl_updated_by"] = str(updated_by or "")
            rec["updated_at"] = rec["acl_updated_at"]
            self._save_unlocked(data)
            return dict(rec)

    def resolve_start(
        self,
        *,
        principal: GatewayPrincipal,
        scope: str = CATALOG_SCOPE_TENANT,
        tenant_id: str = "default",
        bundle_id: str,
        bundle_version: Optional[str],
        flow_id: str,
    ) -> WorkflowCatalogStartSelection:
        scope0 = normalize_catalog_scope(scope)
        tenant0 = safe_principal_component(tenant_id, default="default")
        bid = str(bundle_id or "").strip()
        fid = str(flow_id or "").strip()
        if not bid:
            raise WorkflowCatalogNotFoundError("Catalog bundle_id is required")
        if not fid:
            raise WorkflowCatalogNotFoundError("Catalog flow_id is required")
        bver = str(bundle_version or "").strip() if isinstance(bundle_version, str) and str(bundle_version).strip() else ""
        rec = self.get_record(scope=scope0, tenant_id=tenant0, bundle_id=bid, bundle_version=bver) if bver else self.get_default_record(scope=scope0, tenant_id=tenant0, bundle_id=bid)
        if rec is None:
            suffix = f"@{bver}" if bver else ""
            raise WorkflowCatalogNotFoundError(f"Catalog bundle '{bid}{suffix}' not found")
        status = str(rec.get("status") or "published").strip().lower() or "published"
        if status not in STARTABLE_STATUSES:
            raise WorkflowCatalogStatusError(f"Catalog bundle '{bid}@{rec.get('bundle_version')}' is {status}")
        if not principal_can_run_catalog_record(rec, principal):
            raise WorkflowCatalogForbiddenError(f"Catalog workflow '{bid}@{rec.get('bundle_version')}' is not allowed for this user")
        entrypoints = rec.get("entrypoints") if isinstance(rec.get("entrypoints"), list) else []
        if fid not in {str(ep.get("flow_id") or "").strip() for ep in entrypoints if isinstance(ep, dict)}:
            raise WorkflowCatalogNotFoundError(f"Flow '{fid}' not found in catalog bundle '{bid}@{rec.get('bundle_version')}'")
        return WorkflowCatalogStartSelection(
            scope=scope0,
            tenant_id=tenant0,
            bundle_id=bid,
            bundle_version=str(rec.get("bundle_version") or ""),
            flow_id=fid,
            sha256=str(rec.get("sha256") or ""),
            status=status,
            host_bundle_id=catalog_internal_bundle_id(scope=scope0, tenant_id=tenant0, bundle_id=bid),
        )
