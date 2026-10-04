"""Who owns a workflow bundle, who may see it, and which bundles are archived (DESIGN-v3 §5).

Three operator decisions meet here:

1. OWNERSHIP. A bundle on a user's host is either the gateway's (the shared flows folder,
   loaded with `source_kind` "framework" into a per-user host, or the admin's own registry,
   which IS that shared folder) or the signed-in user's own (their per-user registry,
   `<data>/users/<tenant>/<runtime>/flows`). `owner_of` answers that from where the host
   loaded the file; nothing is guessed from the manifest. Upload and publish additionally
   stamp `metadata.owner = {user_id, tenant_id, at}` (`stamp_owner_into_bundle_bytes`) so a
   future listing can attribute a bundle; legacy bundles keep the folder rule.

2. AVAILABILITY. Admins decide which gateway-owned workflows users see:
   `<data_dir>/config/workflow_availability.json`
   `{version: 1, bundles: {<bundle_id>: {available, updated_by, updated_at}}}`; an absent
   entry means available. `workflow_visible` is THE ONE predicate: `/bundles` (the list every
   app picker reads — Code, Assistant, Flow, the console), the bundle read routes and run
   start all call it. The admin's per-app default workflow is exempt at run start only
   (`agent_default_bundle_ids`), so apps keep working for everyone.

3. NO DELETION. Imported and published bundles are ARCHIVED, never removed:
   `<registry root>/config/workflow_archive.json`
   `{version: 1, bundles: {<bundle_id>: {versions: [..] | "all", archived_by, at}}}`.
   The registry keeps LOADING archived bundles (existing runs and automations resume and
   replay); lists hide them and new run starts refuse them. Shipped bundles can be neither
   archived nor deleted. The gateway-owned archive lives under the gateway data dir; a
   user's own archive lives under their registry root (the folder holding their `flows`).
"""

from __future__ import annotations

import datetime
import io
import json
import os
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

OWNER_GATEWAY = "gateway"
OWNER_USER = "user"

UNAVAILABLE_MESSAGE = "This workflow isn't available to users on this gateway. Ask an admin."
ARCHIVED_MESSAGE = "This workflow is archived: it can't start new runs. Unarchive it on the Workflows page to run it again."
DELETE_GONE_MESSAGE = "Workflows are archived, never deleted: use Archive."
SHIPPED_NOT_ARCHIVABLE_MESSAGE = "Workflows that ship with the gateway can't be archived or deleted. An admin can turn off “Available to users” instead."


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
            f.flush()
        os.replace(tmp, str(path))
        tmp = ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class _JsonBundleStore:
    """A tiny `{version: 1, bundles: {...}}` JSON file, read fresh on every call (the
    files are small and several processes — gateway, TUI-driven API calls — share them)."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            # A corrupt file must never silently flip every bundle back to "available" /
            # "not archived": raise so the caller answers 500 with the path.
            raise RuntimeError(f"{self.path} is not valid JSON; fix or remove it")
        bundles = raw.get("bundles") if isinstance(raw, dict) else None
        return dict(bundles) if isinstance(bundles, dict) else {}

    def _save(self, bundles: Dict[str, Any]) -> None:
        _atomic_write_json(self.path, {"version": 1, "bundles": bundles})


class WorkflowAvailabilityStore(_JsonBundleStore):
    def records(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {str(k): dict(v) for k, v in self._load().items() if isinstance(v, dict)}

    def is_available(self, bundle_id: str) -> bool:
        rec = self.records().get(str(bundle_id or "").strip())
        return True if rec is None else bool(rec.get("available", True))

    def set_available(self, bundle_id: str, available: bool, *, updated_by: str) -> Dict[str, Any]:
        bid = str(bundle_id or "").strip()
        if not bid:
            raise ValueError("bundle_id is required")
        with self._lock:
            bundles = self._load()
            rec = {"available": bool(available), "updated_by": str(updated_by or ""), "updated_at": _utc_now_iso()}
            bundles[bid] = rec
            self._save(bundles)
            return dict(rec)


class WorkflowArchiveStore(_JsonBundleStore):
    def records(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {str(k): dict(v) for k, v in self._load().items() if isinstance(v, dict)}

    def is_archived(self, bundle_id: str, bundle_version: Optional[str] = None) -> bool:
        rec = self.records().get(str(bundle_id or "").strip())
        if rec is None:
            return False
        versions = rec.get("versions")
        if versions == "all":
            return True
        if isinstance(versions, list):
            return bundle_version is not None and str(bundle_version) in {str(v) for v in versions}
        return False

    def archive(self, bundle_id: str, bundle_version: Optional[str], *, archived_by: str) -> Dict[str, Any]:
        bid = str(bundle_id or "").strip()
        if not bid:
            raise ValueError("bundle_id is required")
        with self._lock:
            bundles = self._load()
            rec = dict(bundles.get(bid) or {})
            ver = str(bundle_version or "").strip()
            if not ver:
                versions: Union[str, List[str]] = "all"
            elif rec.get("versions") == "all":
                versions = "all"
            else:
                current = [str(v) for v in (rec.get("versions") or []) if isinstance(rec.get("versions"), list)]
                versions = sorted(set(current) | {ver})
            rec = {"versions": versions, "archived_by": str(archived_by or ""), "at": _utc_now_iso()}
            bundles[bid] = rec
            self._save(bundles)
            return dict(rec)

    def unarchive(self, bundle_id: str, bundle_version: Optional[str], *, all_versions: Iterable[str] = ()) -> bool:
        """Remove the archive mark (one version, or every version). Unarchiving one version of a
        bundle archived as "all" keeps the other loaded versions archived (`all_versions`)."""
        bid = str(bundle_id or "").strip()
        with self._lock:
            bundles = self._load()
            rec = bundles.get(bid)
            if not isinstance(rec, dict):
                return False
            ver = str(bundle_version or "").strip()
            if not ver:
                bundles.pop(bid, None)
            else:
                versions = rec.get("versions")
                current = {str(v) for v in all_versions} if versions == "all" else {str(v) for v in (versions or [])}
                if ver not in current:
                    return False
                left = sorted(current - {ver})
                if left:
                    bundles[bid] = {**rec, "versions": left}
                else:
                    bundles.pop(bid, None)
            self._save(bundles)
            return True


# A workflow's description as its owner wrote it (round 8): the console edits it inline. The
# .flow file is never rewritten (Export keeps returning the original bytes); the text lives
# next to the archive file of the same owner:
#   <registry root>/config/workflow_descriptions.json
#   {version: 1, bundles: {<bundle_id>: {description, updated_by, updated_at}}}
# One description per bundle (every version shows it); an empty text removes the entry, so
# the manifest's own description shows again.
WORKFLOW_DESCRIPTION_MAX_CHARS = 2000
SHIPPED_NOT_EDITABLE_MESSAGE = "Workflows that ship with the gateway keep their own description."


class WorkflowDescriptionStore(_JsonBundleStore):
    def records(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {str(k): dict(v) for k, v in self._load().items() if isinstance(v, dict)}

    def set_description(self, bundle_id: str, description: str, *, updated_by: str) -> Optional[Dict[str, Any]]:
        """Store (or, for an empty text, remove) the owner's description. Returns the record,
        or None when it was removed."""
        bid = str(bundle_id or "").strip()
        if not bid:
            raise ValueError("bundle_id is required")
        text = str(description or "").strip()
        with self._lock:
            bundles = self._load()
            if not text:
                bundles.pop(bid, None)
                self._save(bundles)
                return None
            rec = {"description": text, "updated_by": str(updated_by or ""), "updated_at": _utc_now_iso()}
            bundles[bid] = rec
            self._save(bundles)
            return dict(rec)


def description_store_for(owner_kind: str, *, data_dir: Union[str, Path], bundles_dir: Optional[Union[str, Path]]) -> WorkflowDescriptionStore:
    """The descriptions file of `owner_kind` (same folders as `archive_store_for`)."""
    if owner_kind == OWNER_USER:
        if bundles_dir is None:
            raise RuntimeError("a user-owned bundle without a registry folder")
        return WorkflowDescriptionStore(Path(bundles_dir).expanduser().resolve().parent / "config" / "workflow_descriptions.json")
    return WorkflowDescriptionStore(Path(data_dir) / "config" / "workflow_descriptions.json")


_WORKFLOW_AUDIT_FIELDS = frozenset({"bundle_id", "owner_kind", "actor", "tenant_id", "outcome", "reason", "chars", "previous_chars"})


def audit_workflow_event(event: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Append one typed workflow event to `<data_dir>/audit_log.jsonl` (lengths, never the
    text). Never raises: auditing must not break the caller."""
    try:
        from .security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return None
        entry: Dict[str, Any] = {"ts": _utc_now_iso(), "event": str(event)}
        entry.update({k: v for k, v in fields.items() if k in _WORKFLOW_AUDIT_FIELDS and v is not None})
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
        return entry
    except Exception:
        return None


def availability_store(data_dir: Union[str, Path]) -> WorkflowAvailabilityStore:
    return WorkflowAvailabilityStore(Path(data_dir) / "config" / "workflow_availability.json")


def archive_store_for(owner_kind: str, *, data_dir: Union[str, Path], bundles_dir: Optional[Union[str, Path]]) -> WorkflowArchiveStore:
    """The archive file that governs a bundle of `owner_kind`: the gateway data dir's for
    gateway-owned bundles, the user's registry root (`flows/..`) for the user's own."""
    if owner_kind == OWNER_USER:
        if bundles_dir is None:
            raise RuntimeError("a user-owned bundle without a registry folder")
        return WorkflowArchiveStore(Path(bundles_dir).expanduser().resolve().parent / "config" / "workflow_archive.json")
    return WorkflowArchiveStore(Path(data_dir) / "config" / "workflow_archive.json")


def owner_of(source_meta: Dict[str, Any], *, host_is_shared_registry: bool, principal: Any) -> Dict[str, Any]:
    """`{kind: "gateway"|"user", user_id|None}` from where the host loaded the bundle."""
    kind = str((source_meta or {}).get("source_kind") or "user")
    if kind == "framework" or host_is_shared_registry:
        return {"kind": OWNER_GATEWAY, "user_id": None}
    return {"kind": OWNER_USER, "user_id": str(getattr(principal, "user_id", "") or "") or None}


def workflow_visible(principal: Any, owner: Dict[str, Any], bundle_id: str, *, data_dir: Union[str, Path]) -> bool:
    """THE ONE availability predicate (DESIGN-v3 §5.2): admins see everything, a user sees
    their own bundles and the gateway's bundles an admin left available."""
    if principal is not None and bool(getattr(principal, "is_admin", lambda: False)()):
        return True
    if str((owner or {}).get("kind")) == OWNER_USER:
        return True
    return availability_store(data_dir).is_available(bundle_id)


def stamp_owner_into_bundle_bytes(content: bytes, *, user_id: str, tenant_id: str) -> bytes:
    """Return `.flow` bytes whose manifest carries `metadata.owner = {user_id, tenant_id, at}`
    (additive: every other byte of every other member is copied unchanged). Bytes that are
    not a readable bundle are returned as-is so the registry reports its own error."""
    try:
        src = zipfile.ZipFile(io.BytesIO(content))
        manifest = json.loads(src.read("manifest.json").decode("utf-8"))
    except Exception:
        return content
    if not isinstance(manifest, dict):
        return content
    meta = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    manifest["metadata"] = {**meta, "owner": {"user_id": str(user_id or ""), "tenant_id": str(tenant_id or "default"), "at": _utc_now_iso()}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename == "manifest.json":
                dst.writestr("manifest.json", json.dumps(manifest, indent=2))
            else:
                dst.writestr(info, src.read(info.filename))
    return out.getvalue()
