"""abstractgateway.workspace_policy -- which workspaces an account's agents may use (round 11, operator 2026-10-04).

THREE LEVELS (DESIGN "R11.1 FINAL"); there is NO shared workspace:

1. GATEWAY (admin; ``workspace_policy`` in ``<data>/config/runtime_config.json``) = the ELIGIBLE set:
   ``{posture, default_mode, folders: [{path, mode: ro|rw|deny}]}``. The posture is
   ``allowed_only`` ("Deny everything, allow listed workspaces": only the listed ro/rw rows) or
   ``any_except_denied`` ("Allow everything, refuse listed workspaces": everything at
   ``default_mode`` except the rows). A row's mode is the CAP nobody below may exceed. Built-in
   refusals (the gateway data folder, credential folders) are always refused. Fresh gateway:
   any_except_denied, rw, no rows.
2. ACCOUNT (humans and entities; ``account_workspace_policies[tenant:user]``) = the account's own
   default subset, with the SAME shape: its own posture applied WITHIN the eligible set, its own
   rows (ro|rw|deny; an ro/rw row must be eligible and at most its cap). Absent = follow the
   gateway policy.
3. SESSION (one conversation; ``session_workspaces.py``, stored by the gateway in the owner's plane)
   and RUN (a one-off ``workspace`` object in a start body / automation): same shape, replacing
   the account default for that conversation/run.

A path's effective mode = min(gateway cap, the chosen layer's rule) with deny < ro < rw; within a
list the most specific row wins and nothing re-opens beneath a deny. The run's PRIVATE workspace
(``<data>/workspaces/session-…``) is always read & write for that run and never listed here.

``resolve_effective`` is the ONE computation every enforcement point reads (run starts on every
door, the host's tool sandbox, the run workspace browser, the server file routes). Older stores are
migrated ONCE: the pre-round-9 model (``migrate_store``, v1) and the round-9 model
(``migrate_store_v2``: posture -> any_except_denied, the old shared workspace -> an rw row).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

POLICY_KEY = "workspace_policy"
ACCOUNTS_KEY = "account_workspace_policies"
MIGRATION_MARKER = "workspace_policy_v1"
MIGRATION_MARKER_V2 = "workspace_policy_v2"

GATEWAY_FIELDS = ("posture", "default_mode", "folders")
LAYER_FIELDS = ("posture", "default_mode", "folders")
POSTURES = ("allowed_only", "any_except_denied")
POSTURE_LABELS = {"allowed_only": "Deny everything, allow listed workspaces", "any_except_denied": "Allow everything, refuse listed workspaces"}
MODES = ("ro", "rw", "deny")
GATEWAY_MODES = MODES
_RANK = {"deny": 0, "ro": 1, "rw": 2}
LEVELS = ("run", "session", "account", "gateway")

# Keys of the pre-round-9 model. Stored values are migrated once; writes naming them are refused.
LEGACY_STORE_KEYS = (
    "workspace_root",
    "workspace_mounts",
    "workspace_blocked_paths",
    "client_workspace_scope_overrides",
    "trust_client_launch_folder",
    "workspace_default_mode",
    "user_workspace_policies",
)
LEGACY_WRITE_KEYS = LEGACY_STORE_KEYS + ("workspace_allowed_paths",)
# Field names of earlier drafts, refused by name on the policy routes.
_DRAFT_FIELDS = ("allowed_folders", "never_allowed", "allow_any_folder", "launch_folder_trust", "enabled_folders", "own_folders", "other_sessions", "mode")
LEGACY_MOVED_SENTENCE = (
    "moved to the workspace policy: PUT /api/gateway/workspace/policy {posture, default_mode, folders: [{path, mode}]} "
    "for the gateway, PUT /api/gateway/workspace/policy/{account} {configured, posture, default_mode, folders} for "
    "one account. The access modes, launch-folder trust, the shared workspace and \"Any folder (old clients)\" no "
    "longer exist"
)
SHARED_REMOVED_SENTENCE = (
    "shared_workspace no longer exists: list it as a workspace (folders: [{path, mode: \"rw\"}]). Nothing was saved."
)
READ_ONLY_CAP_SENTENCE = "The gateway allows this workspace read-only"


class WorkspacePolicyError(ValueError):
    """A refused policy write or run payload; the message is ONE sentence the clients show as is.
    ``path`` names the offending path when there is one."""

    def __init__(self, message: str, path: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def detail(self) -> Dict[str, Any]:
        return {"reason": "workspace_refused", "message": self.message, "path": self.path}


# ---------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _real(p: Path) -> Path:
    try:
        return Path(os.path.realpath(str(Path(p).expanduser())))
    except Exception:  # noqa: BLE001
        return Path(p).expanduser()


def _under(child: Path, parent: Path) -> bool:
    try:
        _real(child).relative_to(_real(parent))
        return True
    except ValueError:
        return False


def _under_any(child: Path, parents: Iterable[Path]) -> Optional[Path]:
    for parent in parents:
        if _under(child, parent):
            return parent
    return None


def account_key(tenant_id: Optional[str], user_id: Optional[str]) -> str:
    tenant = str(tenant_id or "default").strip() or "default"
    user = str(user_id or "").strip()
    if not user:
        raise WorkspacePolicyError("An account is required.")
    return f"{tenant}:{user}"


def parse_account(raw: str) -> Tuple[str, str]:
    """``user`` (default tenant) or ``tenant:user`` -> (tenant, user)."""
    text = str(raw or "").strip()
    if ":" in text:
        tenant, _, user = text.partition(":")
    else:
        tenant, user = "default", text
    tenant, user = tenant.strip() or "default", user.strip()
    if not user:
        raise WorkspacePolicyError(f"{raw!r} is not an account: use the account name or tenant:name.")
    return tenant, user


def _store_mod():
    from . import runtime_config

    return runtime_config


def builtin_refused(data_dir: Path) -> List[str]:
    """The gateway data folder and the account's credential folders (never a workspace)."""
    from .workspace_browse import builtin_deny_paths

    return [str(p) for p in builtin_deny_paths(Path(data_dir).expanduser())]


builtin_never_allowed = builtin_refused  # the round-9 name, for the browse/file helpers


def _credential_folders(data_dir: Path) -> List[Path]:
    data_root = _real(Path(data_dir))
    return [Path(p) for p in builtin_refused(data_dir) if _real(Path(p)) != data_root]


def account_plane(data_dir: Path, *, tenant_id: str, user_id: str) -> Tuple[Path, List[Path]]:
    """(this account's own data plane, sub-folders of it that belong to OTHER accounts).

    The operator (default:admin) runs in the gateway's data folder itself, which also holds every
    other account's plane under ``users/``; any other account's plane is ``users/<tenant>/<runtime>``
    (service._config_for_principal)."""
    base = _real(Path(data_dir))
    tenant = str(tenant_id or "default") or "default"
    user = str(user_id or "") or "admin"
    if tenant == "default" and user == "admin":
        return base, [base / "users"]
    runtime_id = user
    try:
        from .users import GatewayUserRegistry, safe_principal_component

        path = base / "auth" / "users.json"
        reg = GatewayUserRegistry(path) if path.exists() else GatewayUserRegistry()
        rec = reg.get_user(user, tenant_id=tenant)
        if rec is not None and str(getattr(rec, "runtime_id", "") or "").strip():
            runtime_id = str(rec.runtime_id)
        runtime_id = safe_principal_component(runtime_id, default=user)
        tenant = safe_principal_component(tenant, default="default")
    except Exception:  # noqa: BLE001 - the user id is the runtime id by default
        pass
    return base / "users" / tenant / runtime_id, []


def in_own_plane(path: Path, data_dir: Path, *, tenant_id: str, user_id: str) -> bool:
    plane, others = account_plane(data_dir, tenant_id=tenant_id, user_id=user_id)
    return _under(path, plane) and _under_any(path, others) is None


def _previous_effective_shared_workspace() -> str:
    """What a gateway before round 9 used when nothing was stored (the legacy env, else the runtime's
    repo-root guess). Used ONLY by the pre-round-9 migration (v1)."""
    return str(_store_mod()._workspace_root_fallback())


def _check_folder(raw: Any, *, what: str) -> str:
    """One workspace with the path-check rules (absolute, existing directory) -> its real path."""
    from .runtime_config import check_workspace_path

    if not isinstance(raw, str):
        raise WorkspacePolicyError(f"{what}: a workspace path (text) is required, got {type(raw).__name__}.")
    out = check_workspace_path(raw)
    if not out.get("valid"):
        raise WorkspacePolicyError(f"{what} {str(raw).strip()!r}: {out.get('sentence') or 'not a directory.'}", path=str(raw).strip())
    return str(_real(Path(out["normalized"])))


def _check_rows(raw: Any, *, what: str, modes: Tuple[str, ...] = MODES) -> List[Dict[str, str]]:
    """Written rows: each ``{path, mode}`` with an explicit mode (a row always says its permission)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkspacePolicyError(f"{what} must be a list of rows {{path, mode}}.")
    names = {"ro": "\"ro\" (read-only)", "rw": "\"rw\" (read & write)", "deny": "\"deny\" (refused)"}
    allowed_txt = " or ".join(names[m] for m in modes)
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkspacePolicyError(f"{what}: each row is {{path, mode}} with mode {allowed_txt}.")
        unknown = sorted(set(item) - {"path", "mode", "cap", "source"})
        if unknown:
            raise WorkspacePolicyError(f"{what}: unknown row field(s) {unknown}; a row is {{path, mode}}.")
        mode = item.get("mode")
        if mode not in modes:
            raise WorkspacePolicyError(f"{what}: mode must be {allowed_txt}; got {mode!r}.", path=str(item.get("path") or "") or None)
        path = _check_folder(item.get("path"), what=f"{what} entry")
        if any(r["path"] == path for r in out):
            raise WorkspacePolicyError(f"{what}: {path!r} is listed twice; nothing was saved.", path=path)
        out.append({"path": path, "mode": mode})
    for row in out:
        for other in out:
            if other is not row and other["mode"] == "deny" and _under(Path(row["path"]), Path(other["path"])):
                raise WorkspacePolicyError(
                    f"{what}: {row['path']!r} is inside the refused workspace {other['path']!r}; nothing re-opens under a refusal.",
                    path=row["path"],
                )
    return out


# ---------------------------------------------------------------- stored shapes


def _rows(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].strip() and item.get("mode") in MODES:
            out.append({"path": item["path"], "mode": item["mode"]})
    return out


def _stored_gateway(stored: Dict[str, Any]) -> Dict[str, Any]:
    raw = stored.get(POLICY_KEY)
    raw = raw if isinstance(raw, dict) else {}
    return {
        "posture": raw.get("posture") if raw.get("posture") in POSTURES else "any_except_denied",
        "default_mode": "ro" if raw.get("default_mode") == "ro" else "rw",
        "folders": _rows(raw.get("folders")),
    }


def stored_layer(raw: Any) -> Optional[Dict[str, Any]]:
    """A stored account/session/run layer, or None when it is not a v2 layer."""
    if not isinstance(raw, dict) or raw.get("posture") not in POSTURES:
        return None
    return {
        "posture": raw["posture"],
        "default_mode": "ro" if raw.get("default_mode") == "ro" else "rw",
        "folders": _rows(raw.get("folders")),
    }


def _stored_accounts(stored: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = stored.get(ACCOUNTS_KEY)
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, entry in raw.items():
        layer = stored_layer(entry)
        if layer is not None:
            out[str(key)] = layer
    return out


def _read(data_dir: Path) -> Dict[str, Any]:
    ensure_migrated(data_dir)
    return _store_mod()._read_store(Path(data_dir))


# ---------------------------------------------------------------- migration (pre-round-9 -> v1, kept verbatim)


def _legacy_present(stored: Dict[str, Any]) -> bool:
    if any(stored.get(k) is not None for k in LEGACY_STORE_KEYS):
        return True
    return bool(str(os.getenv(_store_mod()._ENV_WORKSPACE_MOUNTS) or "").strip())


def _existing_dirs(paths: Iterable[Any], dropped: List[str]) -> List[str]:
    out: List[str] = []
    for item in paths:
        text = str(item or "").strip()
        if not text:
            continue
        p = Path(text).expanduser()
        real = str(_real(p))
        if not p.is_absolute() or not Path(real).is_dir():
            if text not in dropped:
                dropped.append(text)
            continue
        if real not in out:
            out.append(real)
    return out


def _as_list(raw: Any) -> List[str]:
    """A stored path list (list, JSON array text or newline text) as it is."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, str)]
    text = str(raw).strip()
    if text.startswith("["):
        import json

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if isinstance(x, str)]
        except ValueError:
            return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def migrate_store(stored: Dict[str, Any], *, accounts: Iterable[str] = ()) -> Dict[str, Any]:
    """Pure: the old model's keys -> the posture model, deterministically, without widening anyone:

    - shared_workspace = the gateway's current default workspace root (stored > env > default);
    - the old gateway default "allow everything except" -> posture any_except_denied (default rw):
      the old refused folders become ``deny`` rows; every account's refused folders become that
      account's ``deny`` rows;
    - otherwise posture allowed_only: the old extra workspaces and every account's allowed folders
      become ``rw`` rows (each once); an account that did NOT have a folder another account had gets
      a ``deny`` override for it (nobody gains access); the old refused folders become ``deny`` rows
      (gateway's) / account ``deny`` rows (per account); an account in "allow everything except"
      under such a gateway stays allowed_only (conservative) and is listed in ``narrowed_accounts``;
    - "Any folder (old clients)" and launch-folder trust are dropped: previously trusted launch
      folders are NOT added (clients ask the person to add them);
    - folders that no longer exist, and rows that would sit inside a denied folder, are dropped and
      listed under the migration record.

    Returns a NEW store dict: old keys removed, the old block kept verbatim under
    ``_migrated.workspace_policy_v1``."""
    rc = _store_mod()
    out = dict(stored)
    dropped: List[str] = []
    old_block = {k: stored[k] for k in LEGACY_STORE_KEYS if k in stored}

    root_payload = rc._workspace_root_payload(stored)
    shared = str(root_payload.get("value") or "").strip() or _previous_effective_shared_workspace()
    shared = str(_real(Path(shared)))

    raw_mounts = stored.get("workspace_mounts")
    if isinstance(raw_mounts, list):
        mount_paths = [str(e.get("path") or "") for e in raw_mounts if isinstance(e, dict)]
    else:
        mounts_payload = rc._workspace_mounts_payload(stored)
        mount_paths = [str(e.get("path") or "") for e in (mounts_payload.get("entries") or []) if isinstance(e, dict)]

    users_raw = stored.get("user_workspace_policies")
    if isinstance(users_raw, str):
        import json

        try:
            users_raw = json.loads(users_raw)
        except ValueError:
            users_raw = None
    users: Dict[str, Dict[str, Any]] = {}
    if isinstance(users_raw, dict):
        for raw_key, raw_entry in users_raw.items():
            key = rc._normalize_user_policy_key(raw_key, strict=False)
            if key and isinstance(raw_entry, dict):
                users[key] = raw_entry

    gateway_never = _existing_dirs(_as_list(stored.get("workspace_blocked_paths")), dropped)
    gateway_never_kept: List[str] = []
    for p in gateway_never:
        if _under(Path(shared), Path(p)):
            dropped.append(p)  # a deny containing the shared workspace cannot stay (shared always in)
        else:
            gateway_never_kept.append(p)
    deny_paths = [Path(p) for p in gateway_never_kept]

    def _not_denied(paths: List[str]) -> List[str]:
        kept = []
        for p in paths:
            if p == shared or _under(Path(p), Path(shared)):
                continue  # already reachable: the shared workspace
            if _under_any(Path(p), deny_paths) is not None:
                dropped.append(p)
                continue
            kept.append(p)
        return kept

    existing_accounts = sorted(
        {rc._normalize_user_policy_key(a, strict=False) or "" for a in accounts} | {"default:admin"} | set(users)
    )
    existing_accounts = [a for a in existing_accounts if a]
    per_user_allowed = {k: _not_denied(_existing_dirs(_as_list(users[k].get("workspace_allowed_paths")), dropped)) for k in sorted(users)}
    per_user_never = {k: _existing_dirs(_as_list(users[k].get("workspace_blocked_paths")), dropped) for k in sorted(users)}

    default_mode = str(stored.get("workspace_default_mode") or "").strip().lower()
    account_rows: Dict[str, List[Dict[str, str]]] = {k: [] for k in existing_accounts}
    narrowed: List[str] = []
    rows: List[Dict[str, str]] = []
    if default_mode == "blacklist":
        posture = "any_except_denied"
        rows = [{"path": p, "mode": "deny"} for p in gateway_never_kept]
    else:
        posture = "allowed_only"
        mounts = _not_denied(_existing_dirs(mount_paths, dropped))
        extra: List[str] = []
        for key in sorted(per_user_allowed):
            for p in per_user_allowed[key]:
                if p not in mounts and p not in extra:
                    extra.append(p)
        rows = [{"path": p, "mode": "rw"} for p in mounts + extra] + [{"path": p, "mode": "deny"} for p in gateway_never_kept]
        for key in existing_accounts:
            had = set(per_user_allowed.get(key) or [])
            for p in extra:
                if p not in had:
                    account_rows[key].append({"path": p, "mode": "deny"})
            if str((users.get(key) or {}).get("mode") or "").strip().lower() == "blacklist":
                narrowed.append(key)
    for key in sorted(per_user_never):
        for p in per_user_never[key]:
            if p == shared or _under(Path(shared), Path(p)):
                dropped.append(p)
                continue
            if not any(r["path"] == p for r in account_rows.setdefault(key, [])):
                account_rows[key].append({"path": p, "mode": "deny"})

    for k in LEGACY_STORE_KEYS:
        out.pop(k, None)
    out[POLICY_KEY] = {"shared_workspace": shared, "posture": posture, "default_mode": "rw", "folders": rows}
    accounts_out = {k: {"default_mode": None, "folders": v} for k, v in sorted(account_rows.items()) if v}
    if accounts_out:
        out[ACCOUNTS_KEY] = accounts_out
    else:
        out.pop(ACCOUNTS_KEY, None)
    migrated = dict(out.get("_migrated") or {}) if isinstance(out.get("_migrated"), dict) else {}
    migrated[MIGRATION_MARKER] = {
        "at": _now(),
        "old": old_block,
        "env_mounts": str(os.getenv(rc._ENV_WORKSPACE_MOUNTS) or "") or None,
        "dropped_missing_or_conflicting": list(dict.fromkeys(dropped)),
        "narrowed_accounts": narrowed,
        "launch_folder_trust_dropped": "trust_client_launch_folder" in stored
        or any("trust_client_launch_folder" in u for u in users.values()),
    }
    out["_migrated"] = migrated
    return out


def _registry_accounts(data_dir: Path) -> List[str]:
    """Every account (people and entities) of this gateway, as tenant:user."""
    try:
        from .users import GatewayUserRegistry

        path = Path(data_dir) / "auth" / "users.json"
        reg = GatewayUserRegistry(path) if path.exists() else GatewayUserRegistry()
        return [f"{r.tenant_id}:{r.user_id}" for r in reg.list_users()]
    except Exception:  # noqa: BLE001 - no registry = only the operator
        return []


# ---------------------------------------------------------------- migration (round 9 -> round 11, v2)


def _is_v1_policy(stored: Dict[str, Any]) -> bool:
    raw = stored.get(POLICY_KEY)
    if isinstance(raw, dict) and "shared_workspace" in raw:
        return True
    accounts = stored.get(ACCOUNTS_KEY)
    return isinstance(accounts, dict) and any(isinstance(e, dict) and e.get("posture") not in POSTURES for e in accounts.values())


def migrate_store_v2(stored: Dict[str, Any]) -> Dict[str, Any]:
    """Pure: a round-9 store (shared workspace + "accounts narrow only") -> the three-level model
    (operator order, DESIGN R11.1 FINAL), never beyond the new ceiling:

    - the gateway posture becomes "Allow everything, refuse listed workspaces" (any_except_denied),
      keeping its default mode; the old shared workspace becomes one listed ``rw`` row (first);
      every existing row is kept with its mode as the CAP;
    - each account entry (round 9: ``{default_mode: "ro"|null, folders: [{path, mode: ro|deny}]}``)
      becomes a CONFIGURED account layer under the gateway's posture, with its own rows and its
      lowered default (an account with only deny/ro overrides keeps exactly those);
    - a missing directory is dropped and listed under the migration record.

    Returns a NEW store dict; the old blocks are kept verbatim under ``_migrated.workspace_policy_v2``."""
    out = dict(stored)
    old_policy = stored.get(POLICY_KEY) if isinstance(stored.get(POLICY_KEY), dict) else {}
    old_accounts = stored.get(ACCOUNTS_KEY) if isinstance(stored.get(ACCOUNTS_KEY), dict) else {}
    dropped: List[str] = []
    default_mode = "ro" if old_policy.get("default_mode") == "ro" else "rw"
    rows: List[Dict[str, str]] = []
    shared = str(old_policy.get("shared_workspace") or "").strip()
    if shared:
        real = str(_real(Path(shared)))
        if Path(real).is_dir():
            rows.append({"path": real, "mode": "rw"})
        else:
            dropped.append(shared)
    for row in _rows(old_policy.get("folders")):
        if not Path(row["path"]).is_dir():
            dropped.append(row["path"])
            continue
        if any(r["path"] == row["path"] for r in rows):
            continue
        rows.append(dict(row))
    out[POLICY_KEY] = {"posture": "any_except_denied", "default_mode": default_mode, "folders": rows}
    accounts_out: Dict[str, Dict[str, Any]] = {}
    for key, entry in sorted(old_accounts.items()):
        if not isinstance(entry, dict):
            continue
        layer = stored_layer(entry)
        if layer is not None:
            accounts_out[str(key)] = layer  # already the new shape
            continue
        acc_rows = []
        for row in _rows(entry.get("folders")):
            if row["mode"] not in ("ro", "deny"):
                continue
            if not Path(row["path"]).is_dir():
                dropped.append(row["path"])
                continue
            acc_rows.append(dict(row))
        lowered = entry.get("default_mode") == "ro"
        if not acc_rows and not lowered:
            continue
        accounts_out[str(key)] = {
            "posture": "any_except_denied",
            "default_mode": "ro" if lowered else default_mode,
            "folders": acc_rows,
        }
    if accounts_out:
        out[ACCOUNTS_KEY] = accounts_out
    else:
        out.pop(ACCOUNTS_KEY, None)
    migrated = dict(out.get("_migrated") or {}) if isinstance(out.get("_migrated"), dict) else {}
    migrated[MIGRATION_MARKER_V2] = {
        "at": _now(),
        "old": {POLICY_KEY: old_policy, ACCOUNTS_KEY: old_accounts},
        "shared_workspace_row": rows[0]["path"] if shared and rows and rows[0]["mode"] == "rw" and rows[0]["path"] == str(_real(Path(shared))) else None,
        "posture": {"from": old_policy.get("posture") or "allowed_only", "to": "any_except_denied"},
        "configured_accounts": sorted(accounts_out),
        "dropped_missing": list(dict.fromkeys(dropped)),
    }
    out["_migrated"] = migrated
    return out


def ensure_migrated(data_dir: Path) -> bool:
    """Run the one-time migrations the store still needs (pre-round-9 -> v1 -> v2). True when one ran.
    Idempotent: decided from the store's SHAPE (a v2 store has no shared workspace and no v1 account
    entries), so a rerun is a no-op."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    stored = rc._read_store(data_dir)
    legacy = POLICY_KEY not in stored and _legacy_present(stored)
    if not legacy and not _is_v1_policy(stored):
        return False
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        legacy = POLICY_KEY not in stored and _legacy_present(stored)
        if not legacy and not _is_v1_policy(stored):
            return False
        new = stored
        if legacy:
            had_root = bool(str(stored.get("workspace_root") or "").strip()) or any(
                str(os.getenv(n) or "").strip() for n in ("ABSTRACTGATEWAY_WORKSPACE_ROOT", "ABSTRACTGATEWAY_WORKSPACE_DIR")
            )
            new = migrate_store(new, accounts=_registry_accounts(data_dir))
            if not had_root:
                # Nothing was configured: no guessed folder becomes a row.
                new[POLICY_KEY] = {k: v for k, v in new[POLICY_KEY].items() if k != "shared_workspace"}
                new[POLICY_KEY]["shared_workspace"] = ""
        new = migrate_store_v2(new)
        new["_last_changed_by"] = "system:workspace_policy_migration"
        new["_last_changed_at"] = _now()
        rc._write_store(data_dir, new)
    audit_policy_change("migration", actor="system:workspace_policy_migration", changed=[MIGRATION_MARKER_V2])
    return True


# ---------------------------------------------------------------- reads


def _summary_line(posture: str, default: Optional[str], folders: List[Dict[str, Any]]) -> str:
    """One line, byte-exact on every surface: the posture label (with its default mode under
    "Allow everything…"), then each listed workspace with its mode, " · " separated."""
    parts = [POSTURE_LABELS[posture] + (f" ({default})" if posture == "any_except_denied" and default else "")]
    for f in folders:
        parts.append(f"{f['path']} ({'refused' if f['mode'] == 'deny' else f['mode']})")
    return " · ".join(parts)


def gateway_policy(data_dir: Path) -> Dict[str, Any]:
    from .runtime_config import resolve_workspace_builtin_deny_enabled

    g = _stored_gateway(_read(data_dir))
    try:
        max_bytes = int(str(os.getenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", "") or "").strip() or 0)
    except ValueError:
        max_bytes = 0
    g["builtin_refused"] = builtin_refused(data_dir) if resolve_workspace_builtin_deny_enabled(Path(data_dir)) else []
    g["max_attachment_bytes"] = max_bytes if max_bytes > 0 else 25 * 1024 * 1024
    g["summary"] = _summary_line(g["posture"], g["default_mode"], g["folders"])
    return g


def _rule(rows: List[Dict[str, str]], path: Path) -> Optional[str]:
    """The mode of the most specific row containing ``path``; a refusal anywhere above it wins."""
    best: Optional[Dict[str, str]] = None
    for row in rows:
        if _under(path, Path(row["path"])):
            if row["mode"] == "deny":
                return "deny"
            if best is None or len(row["path"]) > len(best["path"]):
                best = row
    return best["mode"] if best else None


def _min(a: str, b: str) -> str:
    return a if _RANK[a] <= _RANK[b] else b


class Caps:
    """The gateway's CAP for any path, for one account (the eligible set: cap != "deny")."""

    def __init__(self, g: Dict[str, Any], data_dir: Path, tenant: str, user: str) -> None:
        self.g, self.data_dir, self.tenant, self.user = g, Path(data_dir), tenant, user
        self.any = g["posture"] == "any_except_denied"
        self.builtin = [_real(Path(p)) for p in g.get("builtin_refused") or []]
        # Rows inside the data folder count only inside this account's own plane.
        self.rows = [r for r in g["folders"] if self._row_usable(r)]

    def _row_usable(self, row: Dict[str, str]) -> bool:
        p = Path(row["path"])
        if _under(p, self.data_dir) and not in_own_plane(p, self.data_dir, tenant_id=self.tenant, user_id=self.user):
            return False
        return True

    def cap(self, path: Path) -> str:
        rp = _real(Path(path))
        hit = _under_any(rp, self.builtin)
        if hit is not None:
            # Only a listed row INSIDE the protected folder (this account's own plane) lifts it.
            inside = [r for r in self.rows if r["mode"] != "deny" and _under(rp, Path(r["path"])) and _under(Path(r["path"]), hit)]
            if not inside:
                return "deny"
            return _rule(self.rows, rp) or "deny"
        rule = _rule(self.rows, rp)
        if rule is not None:
            return rule
        return self.g["default_mode"] if self.any else "deny"


def _layer_mode(layer: Dict[str, Any], path: Path) -> str:
    rule = _rule(layer["folders"], path)
    if rule is not None:
        return rule
    return layer["default_mode"] if layer["posture"] == "any_except_denied" else "deny"


class _Resolver:
    """The effective mode of any path: min(gateway cap, the chosen layer's rule)."""

    def __init__(self, caps: Caps, layer: Optional[Dict[str, Any]]) -> None:
        self.caps, self.layer = caps, layer

    def mode(self, path: Path) -> str:
        rp = _real(Path(path))
        cap = self.caps.cap(rp)
        if self.layer is None:
            return cap
        return _min(cap, _layer_mode(self.layer, rp))


class EffectiveScope(NamedTuple):
    """The effective set for enforcement (Paths, realpath)."""

    posture: str               # "any_except_denied" only when the gateway AND the layer allow everything
    default_mode: Optional[str]
    reach: List[Path]          # ro/rw workspaces
    read_only: List[Path]      # ro workspaces
    writable: List[Path]       # rw workspaces — exceptions inside read-only roots
    deny: List[Path]           # refused rows (gateway + layer)
    builtin: List[Path]
    plane_allow: List[Path]    # reachable workspaces inside this account's own data plane
    resolver: Any
    level: str
    layer: Optional[Dict[str, Any]]

    @property
    def any_folder(self) -> bool:
        return self.posture == "any_except_denied"

    def mode(self, path: Path) -> str:
        return self.resolver.mode(_real(path))

    def refusal(self, path: Path) -> Optional[str]:
        """Why ``path`` is not reachable for this run (None = reachable)."""
        rp = _real(Path(path))
        if _under_any(rp, self.plane_allow) is not None:
            return None
        hit = _under_any(rp, self.builtin)
        if hit is not None:
            return f"{rp} is inside {hit}, which is protected (the gateway's data directory or a credential directory)."
        cap = self.resolver.caps.cap(rp)
        if cap == "deny":
            return f"{rp} is outside the workspaces the gateway allows ({self.resolver.caps.g['summary']})."
        if self.mode(rp) == "deny":
            return f"{rp} is not one of this run's workspaces ({_effective_summary(self)})."
        return None


def _effective_summary(scope: "EffectiveScope") -> str:
    return scope.layer["summary"] if scope.layer and "summary" in scope.layer else ""


def _folders(caps: Caps, res: _Resolver, layer: Optional[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set = set()
    if layer is not None:
        for row in layer["folders"]:
            p = Path(row["path"])
            out.append({"path": row["path"], "mode": res.mode(p), "cap": caps.cap(p), "source": source})
            seen.add(row["path"])
        reachable = [Path(f["path"]) for f in out if f["mode"] != "deny"]
    for row in caps.rows:
        if row["path"] in seen:
            continue
        p = Path(row["path"])
        mode = res.mode(p)
        if layer is not None and layer["posture"] != "any_except_denied" and mode == "deny" and _under_any(p, reachable) is None:
            continue  # implied by the layer's "Deny everything…" posture: not one of its workspaces
        out.append({"path": row["path"], "mode": mode, "cap": caps.cap(p), "source": "gateway"})
        seen.add(row["path"])
    return out


def _effective_dict(
    g: Dict[str, Any], caps: Caps, layer: Optional[Dict[str, Any]], level: str, *, account: str, session_id: Optional[str]
) -> Tuple[Dict[str, Any], _Resolver]:
    res = _Resolver(caps, layer)
    folders = _folders(caps, res, layer, level)
    if layer is None:
        posture, default = g["posture"], (g["default_mode"] if g["posture"] == "any_except_denied" else None)
    else:
        posture = layer["posture"]
        default = None
        if posture == "any_except_denied":
            default = _min(layer["default_mode"], g["default_mode"]) if g["posture"] == "any_except_denied" else layer["default_mode"]
    return (
        {
            "account": account,
            "session_id": session_id,
            "level": level,
            "posture": posture,
            "default_mode": default,
            "folders": folders,
            "summary": _summary_line(posture, default, folders),
            "gateway_summary": g["summary"],
        },
        res,
    )


def _scope_from(eff: Dict[str, Any], g: Dict[str, Any], res: _Resolver, layer: Optional[Dict[str, Any]], data_dir: Path) -> EffectiveScope:
    folders = eff["folders"]
    reach = [_real(Path(f["path"])) for f in folders if f["mode"] in ("ro", "rw")]
    everything = g["posture"] == "any_except_denied" and (layer is None or layer["posture"] == "any_except_denied")
    return EffectiveScope(
        posture="any_except_denied" if everything else "allowed_only",
        default_mode=eff["default_mode"] if everything else None,
        reach=reach,
        read_only=[_real(Path(f["path"])) for f in folders if f["mode"] == "ro"],
        writable=[_real(Path(f["path"])) for f in folders if f["mode"] == "rw"],
        deny=[_real(Path(f["path"])) for f in folders if f["mode"] == "deny"],
        builtin=[Path(p) for p in g["builtin_refused"]],
        plane_allow=[p for p in reach if _under(p, Path(data_dir))],
        resolver=res,
        level=eff["level"],
        layer={**(layer or {}), "summary": eff["summary"]},
    )


_UNSET: Any = object()


def resolve_effective(
    data_dir: Path,
    *,
    tenant_id: Optional[str],
    user_id: Optional[str],
    plane_dir: Optional[Path] = None,
    session_id: Optional[str] = None,
    one_off: Any = _UNSET,
) -> Tuple[Dict[str, Any], EffectiveScope]:
    """(EFFECTIVE payload, enforcement scope) for one account, resolving one-off > session > account
    > gateway. ``one_off`` (a run's ``workspace`` object) is VALIDATED (WorkspacePolicyError on a
    path outside the eligible set or a mode above its cap); stored layers are clamped."""
    data_dir = Path(data_dir)
    g = gateway_policy(data_dir)
    key = account_key(tenant_id or "default", user_id or "admin")
    tenant, _, user = key.partition(":")
    caps = Caps(g, data_dir, tenant, user)
    layer: Optional[Dict[str, Any]] = None
    level = "gateway"
    sid = str(session_id or "").strip() or None
    if one_off is not _UNSET and one_off is not None:
        layer, level = validate_layer(caps, one_off, what="workspace"), "run"
    if layer is None and sid and plane_dir is not None:
        from .session_workspaces import session_layer

        layer = session_layer(plane_dir, sid)
        if layer is not None:
            level = "session"
    if layer is None:
        layer = _stored_accounts(_read(data_dir)).get(key)
        if layer is not None:
            level = "account"
    eff, res = _effective_dict(g, caps, layer, level, account=key, session_id=sid)
    return eff, _scope_from(eff, g, res, layer, data_dir)


def effective_policy(data_dir: Path, *, tenant_id: Optional[str], user_id: Optional[str], **kw: Any) -> Dict[str, Any]:
    """The EFFECTIVE payload (see ``resolve_effective``)."""
    return resolve_effective(data_dir, tenant_id=tenant_id, user_id=user_id, **kw)[0]


def effective_folder_paths(data_dir: Path, *, tenant_id: Optional[str], user_id: Optional[str], **kw: Any) -> EffectiveScope:
    """The enforcement scope (see ``resolve_effective``)."""
    return resolve_effective(data_dir, tenant_id=tenant_id, user_id=user_id, **kw)[1]


def account_layer_effective(data_dir: Path, *, tenant_id: str, user_id: str) -> Dict[str, Any]:
    """EFFECTIVE at the ACCOUNT level (the account default; never a session's choice)."""
    return resolve_effective(data_dir, tenant_id=tenant_id, user_id=user_id)[0]


def caps_for(data_dir: Path, *, tenant_id: str, user_id: str) -> Caps:
    g = gateway_policy(data_dir)
    key = account_key(tenant_id or "default", user_id or "admin")
    tenant, _, user = key.partition(":")
    return Caps(g, Path(data_dir), tenant, user)


def account_policy(data_dir: Path, *, tenant_id: str, user_id: str) -> Dict[str, Any]:
    key = account_key(tenant_id, user_id)
    layer = _stored_accounts(_read(data_dir)).get(key)
    return _layer_view(layer, gateway_policy(data_dir), account=key)


def _layer_view(layer: Optional[Dict[str, Any]], g: Dict[str, Any], **ids: Any) -> Dict[str, Any]:
    """The stored layer as a client shows it; not configured = the gateway's posture and default
    with no rows (the display base: "follow the gateway policy")."""
    if layer is None:
        return {**ids, "configured": False, "posture": g["posture"], "default_mode": g["default_mode"], "folders": []}
    return {**ids, "configured": True, "posture": layer["posture"], "default_mode": layer["default_mode"], "folders": [dict(r) for r in layer["folders"]]}


# ---------------------------------------------------------------- writes


def audit_policy_change(scope: str, *, actor: str, changed: List[str], account: Optional[str] = None, **extra: Any) -> None:
    """One typed `workspace_policy_changed` line in the gateway audit log. Never raises."""
    try:
        from .security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return
        entry: Dict[str, Any] = {"ts": _now(), "event": "workspace_policy_changed", "scope": scope, "actor": actor, "changed": list(changed)}
        if account:
            entry["account"] = account
        entry.update({k: v for k, v in extra.items() if v is not None})
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        return


def _refuse_unknown(changes: Dict[str, Any], fields: Tuple[str, ...], *, what: str) -> None:
    if "shared_workspace" in changes:
        raise WorkspacePolicyError(SHARED_REMOVED_SENTENCE)
    unknown = sorted(str(k) for k in changes if k not in fields)
    if unknown:
        old = [k for k in unknown if k in LEGACY_WRITE_KEYS or k in _DRAFT_FIELDS or k == "workspace_access_mode"]
        hint = (
            " Two dimensions only: the posture with its workspace rows {path, mode}, and each workspace's mode. "
            "Separate allowed and refused lists, launch-folder trust, the access modes and \"Any folder (old "
            "clients)\" no longer exist."
            if old
            else ""
        )
        raise WorkspacePolicyError(f"Unknown {what} field(s) {unknown}; nothing was saved. Accepted: {list(fields)}.{hint}")


def _check_posture(raw: Any) -> str:
    if raw not in POSTURES:
        raise WorkspacePolicyError(
            "posture must be \"allowed_only\" (Deny everything, allow listed workspaces) or \"any_except_denied\" "
            f"(Allow everything, refuse listed workspaces); got {raw!r}."
        )
    return str(raw)


def _check_default(raw: Any) -> str:
    if raw not in ("ro", "rw"):
        raise WorkspacePolicyError(f"default_mode must be \"ro\" or \"rw\"; got {raw!r}.")
    return str(raw)


def validate_layer(caps: Caps, raw: Any, *, what: str = "workspaces", base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """An account/session/run layer, validated against the gateway's eligible set and caps.
    Named fields replace ``base`` (default: the gateway's posture and default, no rows)."""
    if not isinstance(raw, dict):
        raise WorkspacePolicyError(f"{what} must be an object {{posture, default_mode, folders: [{{path, mode}}]}}.")
    changes = {k: v for k, v in raw.items() if k not in ("ok", "account", "session_id", "configured", "level", "summary", "gateway_summary", "cap", "source")}
    _refuse_unknown(changes, LAYER_FIELDS, what=what)
    g = caps.g
    layer = dict(base) if base else {"posture": g["posture"], "default_mode": g["default_mode"], "folders": []}
    if "posture" in changes:
        layer["posture"] = _check_posture(changes["posture"])
    if "default_mode" in changes:
        layer["default_mode"] = _check_default(changes["default_mode"])
    if "folders" in changes:
        layer["folders"] = _check_rows(changes["folders"], what="Workspaces")
    if (
        layer["posture"] == "any_except_denied"
        and layer["default_mode"] == "rw"
        and g["posture"] == "any_except_denied"
        and g["default_mode"] == "ro"
        and "default_mode" in changes
    ):
        raise WorkspacePolicyError("The gateway allows unlisted workspaces read-only, so the default cannot be read & write.")
    for row in layer["folders"]:
        if row["mode"] == "deny":
            continue  # a refusal never widens
        cap = caps.cap(Path(row["path"]))
        if cap == "deny":
            raise WorkspacePolicyError(
                f"{row['path']} is outside the workspaces the gateway allows ({g['summary']}).", path=row["path"]
            )
        if _RANK[row["mode"]] > _RANK[cap]:
            raise WorkspacePolicyError(f"{READ_ONLY_CAP_SENTENCE}: {row['path']}.", path=row["path"])
    return {"posture": layer["posture"], "default_mode": layer["default_mode"], "folders": layer["folders"]}


def write_gateway_policy(data_dir: Path, changes: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    """Partial update: named fields replace, unnamed keep. Validated as a whole before anything lands."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    changes = dict(changes or {})
    changes.pop("ok", None)
    for ro in ("builtin_refused", "builtin_refused_hidden", "max_attachment_bytes", "summary"):
        changes.pop(ro, None)  # read-only echoes of a GET are tolerated, never stored
    _refuse_unknown(changes, GATEWAY_FIELDS, what="gateway workspace policy")
    ensure_migrated(data_dir)
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        g = _stored_gateway(stored)
        if "posture" in changes:
            g["posture"] = _check_posture(changes["posture"])
        if "default_mode" in changes:
            g["default_mode"] = _check_default(changes["default_mode"])
        if "folders" in changes:
            g["folders"] = _check_rows(changes["folders"], what="Workspaces")
        creds = _credential_folders(data_dir)
        for row in g["folders"]:
            if row["mode"] != "deny" and _under_any(Path(row["path"]), creds) is not None:
                raise WorkspacePolicyError(f"{row['path']} is a protected credential directory; it is never a workspace.", path=row["path"])
        stored[POLICY_KEY] = {k: g[k] for k in GATEWAY_FIELDS}
        stored["_last_changed_by"] = str(actor)
        stored["_last_changed_at"] = _now()
        rc._write_store(data_dir, stored)
    audit_policy_change("gateway", actor=actor, changed=sorted(changes))
    return gateway_policy(data_dir)


def _configured_flag(changes: Dict[str, Any]) -> Optional[bool]:
    if "configured" not in changes:
        return None
    raw = changes["configured"]
    if not isinstance(raw, bool):
        raise WorkspacePolicyError(f"configured must be true or false; got {raw!r}.")
    if raw is False and any(k in changes for k in LAYER_FIELDS):
        raise WorkspacePolicyError("configured: false follows the level above; send it without posture, default_mode or folders.")
    return raw


def write_layer(caps: Caps, current: Optional[Dict[str, Any]], changes: Dict[str, Any], *, what: str) -> Optional[Dict[str, Any]]:
    """The new stored layer for a PUT (None = not configured)."""
    changes = dict(changes or {})
    flag = _configured_flag(changes)
    if flag is False:
        return None
    rest = {k: v for k, v in changes.items() if k != "configured"}
    if not rest and flag is None:
        return current
    return validate_layer(caps, rest, what=what, base=current)


def write_account_policy(data_dir: Path, *, tenant_id: str, user_id: str, changes: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """PUT one account's default subset: ``{configured: false}`` = follow the gateway policy; any of
    {posture, default_mode, folders} configures it (unnamed fields keep the stored value, else start
    from the gateway's posture and default with no rows)."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    key = account_key(tenant_id, user_id)
    changes = dict(changes or {})
    changes.pop("ok", None)
    changes.pop("account", None)
    ensure_migrated(data_dir)
    caps = caps_for(data_dir, tenant_id=tenant_id, user_id=user_id)
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        accounts = _stored_accounts(stored)
        new = write_layer(caps, accounts.get(key), changes, what="account workspaces")
        if new is None:
            accounts.pop(key, None)
        else:
            accounts[key] = new
        if accounts:
            stored[ACCOUNTS_KEY] = accounts
        else:
            stored.pop(ACCOUNTS_KEY, None)
        stored["_last_changed_by"] = str(actor)
        stored["_last_changed_at"] = _now()
        rc._write_store(data_dir, stored)
    audit_policy_change("account", actor=actor, changed=sorted(changes), account=key)
    return account_policy(data_dir, tenant_id=tenant_id, user_id=user_id)


def legacy_paths_to_layer(caps: Caps, paths: Iterable[Any]) -> Dict[str, Any]:
    """The round-9 automation list ``workspace_allowed_paths`` as a run layer: "Deny everything,
    allow listed workspaces" with each eligible path at its cap (ineligible paths are left out)."""
    rows: List[Dict[str, str]] = []
    for item in paths:
        text = str(item or "").strip()
        if not text or not Path(text).expanduser().is_absolute():
            continue
        real = str(_real(Path(text)))
        if not Path(real).is_dir() or any(r["path"] == real for r in rows):
            continue
        cap = caps.cap(Path(real))
        if cap != "deny":
            rows.append({"path": real, "mode": cap})
    return {"posture": "allowed_only", "default_mode": "rw", "folders": rows}


def clamp_layer(caps: Caps, raw: Any) -> Optional[Dict[str, Any]]:
    """A one-off layer CLAMPED (never refused) to the gateway's eligible set and caps: what an
    in-process door (automation occurrence, host start) applies to a stored or forwarded payload.
    Rows that are not existing directories or are outside the eligible set are dropped, modes above
    the cap lowered; a malformed object is None (the run follows the next level)."""
    if not isinstance(raw, dict) or raw.get("posture") not in POSTURES:
        return None
    default = "ro" if raw.get("default_mode") == "ro" else "rw"
    if raw["posture"] == "any_except_denied" and caps.any and caps.g["default_mode"] == "ro":
        default = "ro"
    rows: List[Dict[str, str]] = []
    for item in raw.get("folders") or []:
        if not isinstance(item, dict) or item.get("mode") not in MODES or not isinstance(item.get("path"), str):
            continue
        p = Path(item["path"]).expanduser()
        if not p.is_absolute() or not p.is_dir():
            continue
        real = str(_real(p))
        if any(r["path"] == real for r in rows):
            continue
        mode = item["mode"]
        if mode != "deny":
            cap = caps.cap(Path(real))
            if cap == "deny":
                continue
            mode = _min(mode, cap)
        rows.append({"path": real, "mode": mode})
    return {"posture": raw["posture"], "default_mode": default, "folders": rows}
