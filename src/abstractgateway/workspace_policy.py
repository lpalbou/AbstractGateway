"""abstractgateway.workspace_policy -- which folders an account's agents may use (round 9, operator 2026-10-04).

The workspace model is the tools model: the ADMIN allows, the ACCOUNT fine-tunes within what is allowed.

Gateway policy (admin; stored under ``workspace_policy`` in ``<data>/config/runtime_config.json``):

- ``shared_workspace``: exactly one folder, always present, the root every agent / session / entity
  starts from (the gateway still makes per-session folders in its data folder, as before).
- ``allowed_folders``: extra folders an account may switch on.
- ``allow_any_folder``: off by default; on = accounts may add folders of their own.
- ``never_allowed``: a gateway-wide deny list; it applies on top of everything and always wins.
- ``launch_folder_trust``: a run may work in the folder its app was started from (default on).

Account policy (human, entity or the admin's own; stored under ``account_workspace_policies``
keyed ``tenant:user``):

- ``enabled_folders``: the admin-allowed extras this account switched ON (default none);
  always a subset of ``allowed_folders``.
- ``own_folders``: the account's own folders, honoured only while ``allow_any_folder`` is on.

``effective_policy`` is the ONE computation every enforcement point reads (run starts, the host's
tool sandbox, the run workspace browser, the server file routes): the shared workspace, then the
enabled extras, then the own folders, minus anything inside a never-allowed folder.

The old model (whitelist/blacklist "access mode", per-user allow/deny lists and trust overrides,
"Any folder (old clients)") is migrated ONCE, deterministically (``migrate_store``), and its keys are
refused on write afterwards. No retro-compatibility: every client runs the new versions.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

POLICY_KEY = "workspace_policy"
ACCOUNTS_KEY = "account_workspace_policies"
MIGRATION_MARKER = "workspace_policy_v1"

GATEWAY_FIELDS = ("shared_workspace", "allowed_folders", "allow_any_folder", "never_allowed", "launch_folder_trust")
ACCOUNT_FIELDS = ("enabled_folders", "own_folders")

# Keys of the old model. Stored values are migrated once; writes naming them are refused.
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
LEGACY_MOVED_SENTENCE = (
    "moved to the workspace policy: PUT /api/gateway/workspace/policy {shared_workspace, allowed_folders, "
    "allow_any_folder, never_allowed, launch_folder_trust} for the gateway, PUT /api/gateway/workspace/policy/{account} "
    "{enabled_folders, own_folders} for one account. The access modes and \"Any folder (old clients)\" no longer exist"
)


class WorkspacePolicyError(ValueError):
    """A refused policy write; the message is a sentence the console shows as is."""


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
        raise WorkspacePolicyError("an account is required")
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
        raise WorkspacePolicyError(f"{raw!r} is not an account: use the account name or tenant:name")
    return tenant, user


def _store_mod():
    from . import runtime_config

    return runtime_config


def builtin_never_allowed(data_dir: Path) -> List[str]:
    """The gateway data folder and the account's credential folders (never a workspace)."""
    from .workspace_browse import builtin_deny_paths

    return [str(p) for p in builtin_deny_paths(Path(data_dir).expanduser())]


def _default_shared_workspace() -> str:
    return str(_store_mod()._workspace_root_fallback())


def _check_folder(raw: Any, *, what: str) -> str:
    """One folder with the path-check rules (absolute, existing directory) -> its real path."""
    from .runtime_config import check_workspace_path

    if not isinstance(raw, str):
        raise WorkspacePolicyError(f"{what}: a folder path (text) is required, got {type(raw).__name__}")
    out = check_workspace_path(raw)
    if not out.get("valid"):
        raise WorkspacePolicyError(f"{what} {str(raw).strip()!r}: {out.get('sentence') or 'not a folder'}")
    return str(_real(Path(out["normalized"])))


def _check_folder_list(raw: Any, *, what: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkspacePolicyError(f"{what} must be a list of folder paths")
    out: List[str] = []
    for item in raw:
        path = _check_folder(item, what=f"{what} entry")
        if path in out:
            raise WorkspacePolicyError(f"{what}: {path!r} is listed twice; nothing was saved.")
        out.append(path)
    return out


def _strict_bool(raw: Any, *, what: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise WorkspacePolicyError(f"{what} must be true or false (got {raw!r})")


# ---------------------------------------------------------------- stored shapes


def _stored_gateway(stored: Dict[str, Any]) -> Dict[str, Any]:
    raw = stored.get(POLICY_KEY)
    raw = raw if isinstance(raw, dict) else {}
    shared = str(raw.get("shared_workspace") or "").strip() or _default_shared_workspace()
    return {
        "shared_workspace": str(_real(Path(shared))),
        "allowed_folders": [str(x) for x in (raw.get("allowed_folders") or []) if isinstance(x, str) and x.strip()],
        "allow_any_folder": raw.get("allow_any_folder") is True,
        "never_allowed": [str(x) for x in (raw.get("never_allowed") or []) if isinstance(x, str) and x.strip()],
        "launch_folder_trust": raw.get("launch_folder_trust") is not False,
    }


def _stored_accounts(stored: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    raw = stored.get(ACCOUNTS_KEY)
    out: Dict[str, Dict[str, List[str]]] = {}
    if not isinstance(raw, dict):
        return out
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        out[str(key)] = {
            field: [str(x) for x in (entry.get(field) or []) if isinstance(x, str) and x.strip()] for field in ACCOUNT_FIELDS
        }
    return out


def _read(data_dir: Path) -> Dict[str, Any]:
    ensure_migrated(data_dir)
    return _store_mod()._read_store(Path(data_dir))


# ---------------------------------------------------------------- migration


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
    """Pure: the old model's keys -> the new model, deterministically (operator rule, round 9):

    - shared_workspace = the gateway's current default workspace root (stored > env > default);
    - allowed_folders = the gateway's extra workspaces (mounts), then every account's allowed
      folders (accounts in key order), each once. Switches ON for the accounts that had them:
      an account's own allowed folders, and the gateway's extra workspaces for every account
      that existed (``accounts``, plus default:admin) — the old gateway list applied to all of
      them, so nobody loses a folder; accounts created later start with the extras off;
    - blacklist ("allow everything except") as the gateway default OR for any account ->
      allow_any_folder ON;
    - never_allowed = the gateway's refused folders, then every account's refused folders
      (deny wins: a per-account refusal becomes gateway-wide rather than being lost);
    - "Any folder (old clients)" (client_workspace_scope_overrides) is dropped, gateway-wide and
      per account; per-account launch-folder trust is dropped (one gateway switch);
    - launch_folder_trust = the gateway's trust (default on);
    - folders that no longer exist are dropped and listed under the migration record.

    Returns a NEW store dict: old keys removed, the old block kept verbatim under
    ``_migrated.workspace_policy_v1`` with what was dropped."""
    rc = _store_mod()
    out = dict(stored)
    dropped: List[str] = []
    old_block = {k: stored[k] for k in LEGACY_STORE_KEYS if k in stored}

    root_payload = rc._workspace_root_payload(stored)
    shared = str(root_payload.get("value") or "").strip() or _default_shared_workspace()
    shared = str(_real(Path(shared)))

    raw_mounts = stored.get("workspace_mounts")
    if isinstance(raw_mounts, list):
        # Read the stored entries as they are, so a folder that no longer exists is REPORTED as dropped.
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
        # The stored entries as they are (keys folded to tenant:user), so missing folders are REPORTED as dropped.
        for raw_key, raw_entry in users_raw.items():
            key = rc._normalize_user_policy_key(raw_key, strict=False)
            if key and isinstance(raw_entry, dict):
                users[key] = raw_entry

    allowed = _existing_dirs(mount_paths, dropped)
    gateway_extras = [p for p in allowed if p != shared]
    existing_accounts = sorted({rc._normalize_user_policy_key(a, strict=False) or "" for a in accounts} | {"default:admin"} | set(users))
    existing_accounts = [a for a in existing_accounts if a]
    per_account: Dict[str, Dict[str, List[str]]] = {}
    for key in sorted(users):
        own_allowed = _existing_dirs(_as_list(users[key].get("workspace_allowed_paths")), dropped)
        for p in own_allowed:
            if p not in allowed:
                allowed.append(p)
        per_account[key] = {"own_allowed": own_allowed}
    account_entries: Dict[str, Dict[str, List[str]]] = {}
    for key in existing_accounts:
        enabled = list(gateway_extras)
        for p in (per_account.get(key) or {}).get("own_allowed", []):
            if p != shared and p not in enabled:
                enabled.append(p)
        if enabled:
            account_entries[key] = {"enabled_folders": enabled, "own_folders": []}
    allowed = [p for p in allowed if p != shared]

    default_mode = str(stored.get("workspace_default_mode") or "").strip().lower()
    allow_any = default_mode == "blacklist" or any(str(e.get("mode") or "").strip().lower() == "blacklist" for e in users.values())

    never = _existing_dirs(_as_list(stored.get("workspace_blocked_paths")), dropped)
    for key in sorted(users):
        for p in _existing_dirs(_as_list(users[key].get("workspace_blocked_paths")), dropped):
            if p not in never:
                never.append(p)
    # Shared always present: a refused folder that contains the shared workspace cannot stay.
    never_kept = []
    for p in never:
        if _under(Path(shared), Path(p)):
            dropped.append(p)
        else:
            never_kept.append(p)

    trust_raw = stored.get("trust_client_launch_folder")
    trust = True if trust_raw is None else bool(rc._bool(trust_raw, True))

    for k in LEGACY_STORE_KEYS:
        out.pop(k, None)
    out[POLICY_KEY] = {
        "shared_workspace": shared,
        "allowed_folders": allowed,
        "allow_any_folder": bool(allow_any),
        "never_allowed": never_kept,
        "launch_folder_trust": trust,
    }
    if account_entries:
        out[ACCOUNTS_KEY] = account_entries
    else:
        out.pop(ACCOUNTS_KEY, None)
    migrated = dict(out.get("_migrated") or {}) if isinstance(out.get("_migrated"), dict) else {}
    migrated[MIGRATION_MARKER] = {
        "at": _now(),
        "old": old_block,
        "env_mounts": str(os.getenv(rc._ENV_WORKSPACE_MOUNTS) or "") or None,
        "dropped_missing_or_conflicting": dropped,
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


def ensure_migrated(data_dir: Path) -> bool:
    """Run the one-time migration when the store still holds the old model. True when it ran."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    stored = rc._read_store(data_dir)
    if POLICY_KEY in stored or not _legacy_present(stored):
        return False
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        if POLICY_KEY in stored or not _legacy_present(stored):
            return False
        new = migrate_store(stored, accounts=_registry_accounts(data_dir))
        new["_last_changed_by"] = "system:workspace_policy_migration"
        new["_last_changed_at"] = _now()
        rc._write_store(data_dir, new)
    audit_policy_change("migration", actor="system:workspace_policy_migration", changed=list(GATEWAY_FIELDS))
    return True


# ---------------------------------------------------------------- reads


def gateway_policy(data_dir: Path) -> Dict[str, Any]:
    from .runtime_config import resolve_workspace_builtin_deny_enabled

    stored = _read(data_dir)
    g = _stored_gateway(stored)
    try:
        max_bytes = int(str(os.getenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", "") or "").strip() or 0)
    except ValueError:
        max_bytes = 0
    g["builtin_never_allowed"] = builtin_never_allowed(data_dir) if resolve_workspace_builtin_deny_enabled(Path(data_dir)) else []
    g["max_attachment_bytes"] = max_bytes if max_bytes > 0 else 25 * 1024 * 1024
    return g


def account_policy(data_dir: Path, *, tenant_id: str, user_id: str) -> Dict[str, Any]:
    key = account_key(tenant_id, user_id)
    entry = _stored_accounts(_read(data_dir)).get(key) or {"enabled_folders": [], "own_folders": []}
    return {"account": key, "enabled_folders": list(entry.get("enabled_folders") or []), "own_folders": list(entry.get("own_folders") or [])}


def _summary(shared: str, folders: List[Dict[str, str]], never: List[str], own_inactive: bool) -> str:
    extras = [f for f in folders if f["source"] != "shared"]
    own = [f for f in extras if f["source"] == "own"]
    name = Path(shared).name or shared
    # Parent ruling (round 9): each conversation keeps its PRIVATE session folder in the account's
    # data plane; the shared workspace is the root every run can always reach.
    text = f"Private session folder + Shared workspace ({name})"
    if extras:
        text += f" + {len(extras)} folder{'s' if len(extras) != 1 else ''}"
        if own:
            # Neutral: an admin reads this line about another account too.
            text += f" ({len(own)} own folder{'s' if len(own) != 1 else ''})"
    text += "."
    if never:
        text += f" Never: {len(never)} folder{'s' if len(never) != 1 else ''}."
    if own_inactive:
        text += " Own folders are off: the admin no longer allows any folder."
    return text


def effective_policy(data_dir: Path, *, tenant_id: Optional[str], user_id: Optional[str]) -> Dict[str, Any]:
    """What this account's agents may use, computed server-side (the only input of enforcement)."""
    g = gateway_policy(data_dir)
    key = account_key(tenant_id, user_id)
    acc = _stored_accounts(_read(data_dir)).get(key) or {"enabled_folders": [], "own_folders": []}
    never_paths = [Path(p) for p in g["never_allowed"]]
    builtin = [Path(p) for p in g["builtin_never_allowed"]]
    enabled = set(acc.get("enabled_folders") or [])

    folders: List[Dict[str, str]] = [{"path": g["shared_workspace"], "source": "shared"}]
    seen = {g["shared_workspace"]}
    available: List[Dict[str, Any]] = []
    for p in g["allowed_folders"]:
        on = p in enabled
        blocked = _under_any(Path(p), never_paths) is not None
        available.append({"path": p, "enabled": on, "never_allowed": blocked})
        if on and not blocked and p not in seen and Path(p).is_dir():
            folders.append({"path": p, "source": "allowed"})
            seen.add(p)
    own = list(acc.get("own_folders") or [])
    own_inactive = bool(own) and not g["allow_any_folder"]
    if g["allow_any_folder"]:
        for p in own:
            if p in seen or _under_any(Path(p), never_paths) or _under_any(Path(p), builtin) or not Path(p).is_dir():
                continue
            folders.append({"path": p, "source": "own"})
            seen.add(p)
    never_all = list(dict.fromkeys(g["never_allowed"] + g["builtin_never_allowed"]))
    return {
        "account": key,
        "shared_workspace": g["shared_workspace"],
        "folders": folders,
        "available_folders": available,
        "own_folders": own,
        "own_folders_allowed": bool(g["allow_any_folder"]),
        "own_folders_inactive": own_inactive,
        "never_allowed": never_all,
        "launch_folder_trust": bool(g["launch_folder_trust"]),
        "summary": _summary(g["shared_workspace"], folders, g["never_allowed"], own_inactive),
    }


def effective_folder_paths(
    data_dir: Path, *, tenant_id: Optional[str], user_id: Optional[str]
) -> Tuple[List[Path], List[Path], List[Path], bool]:
    """(effective folders [shared first], the gateway's never-allowed folders, the built-in
    never-allowed folders [data folder + credential folders], launch_folder_trust) as Paths.
    The built-in ones are enforced by the host as deny prefixes with the run's own folder as the
    one exception (run_workspace_guard), so they are kept apart from the gateway's list."""
    eff = effective_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
    g = gateway_policy(data_dir)
    return (
        [Path(f["path"]) for f in eff["folders"]],
        [Path(p) for p in g["never_allowed"]],
        [Path(p) for p in g["builtin_never_allowed"]],
        bool(eff["launch_folder_trust"]),
    )


# ---------------------------------------------------------------- writes


def audit_policy_change(scope: str, *, actor: str, changed: List[str], account: Optional[str] = None) -> None:
    """One typed `workspace_policy_changed` line in the gateway audit log. Never raises."""
    try:
        import json

        from .security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return
        entry: Dict[str, Any] = {"ts": _now(), "event": "workspace_policy_changed", "scope": scope, "actor": actor, "changed": list(changed)}
        if account:
            entry["account"] = account
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        return


def _refuse_unknown(changes: Dict[str, Any], fields: Tuple[str, ...], *, what: str) -> None:
    unknown = sorted(str(k) for k in changes if k not in fields)
    if unknown:
        legacy = [k for k in unknown if k in LEGACY_WRITE_KEYS or k in ("mode", "workspace_access_mode")]
        hint = " The access modes and \"Any folder (old clients)\" no longer exist." if legacy else ""
        raise WorkspacePolicyError(f"unknown {what} field(s) {unknown}; nothing was saved. Accepted: {list(fields)}.{hint}")


def write_gateway_policy(data_dir: Path, changes: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    """Partial update: named fields replace, unnamed keep. Validated as a whole before anything lands."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    changes = dict(changes or {})
    changes.pop("ok", None)
    for ro in ("builtin_never_allowed", "max_attachment_bytes"):
        changes.pop(ro, None)  # read-only echoes of a GET are tolerated, never stored
    _refuse_unknown(changes, GATEWAY_FIELDS, what="gateway workspace policy")
    ensure_migrated(data_dir)
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        g = _stored_gateway(stored)
        if "shared_workspace" in changes:
            raw = changes["shared_workspace"]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                raise WorkspacePolicyError("The shared workspace is required: choose a folder (it cannot be empty).")
            g["shared_workspace"] = _check_folder(raw, what="Shared workspace")
        if "allowed_folders" in changes:
            g["allowed_folders"] = _check_folder_list(changes["allowed_folders"], what="Allowed folders")
        if "never_allowed" in changes:
            g["never_allowed"] = _check_folder_list(changes["never_allowed"], what="Never allowed")
        if "allow_any_folder" in changes:
            g["allow_any_folder"] = _strict_bool(changes["allow_any_folder"], what="allow_any_folder")
        if "launch_folder_trust" in changes:
            g["launch_folder_trust"] = _strict_bool(changes["launch_folder_trust"], what="launch_folder_trust")

        shared = Path(g["shared_workspace"])
        builtin = [Path(p) for p in builtin_never_allowed(data_dir)]
        hit = _under_any(shared, builtin)
        if hit is not None:
            raise WorkspacePolicyError(
                f"Shared workspace {str(shared)!r} is inside {str(hit)!r} (the gateway's data folder or a credential "
                "folder), which is never a workspace."
            )
        for p in g["never_allowed"]:
            if _under(shared, Path(p)):
                raise WorkspacePolicyError(
                    f"Never allowed {p!r} contains the shared workspace {str(shared)!r}; the shared workspace is always "
                    "allowed. Refuse a folder inside it, or move the shared workspace first."
                )
        never_paths = [Path(p) for p in g["never_allowed"]]
        for p in g["allowed_folders"]:
            hit = _under_any(Path(p), never_paths) or _under_any(Path(p), builtin)
            if hit is not None:
                raise WorkspacePolicyError(f"Allowed folder {p!r} is inside never-allowed {str(hit)!r}; never allowed wins.")
        g["allowed_folders"] = [p for p in g["allowed_folders"] if p != g["shared_workspace"]]

        stored[POLICY_KEY] = {k: g[k] for k in GATEWAY_FIELDS}
        # An extra the admin no longer allows is no longer switched on anywhere.
        accounts = _stored_accounts(stored)
        allowed_set = set(g["allowed_folders"])
        for key, entry in accounts.items():
            entry["enabled_folders"] = [p for p in entry.get("enabled_folders") or [] if p in allowed_set]
        accounts = {k: v for k, v in accounts.items() if v.get("enabled_folders") or v.get("own_folders")}
        if accounts:
            stored[ACCOUNTS_KEY] = accounts
        else:
            stored.pop(ACCOUNTS_KEY, None)
        stored["_last_changed_by"] = str(actor)
        stored["_last_changed_at"] = _now()
        rc._write_store(data_dir, stored)
    audit_policy_change("gateway", actor=actor, changed=sorted(changes))
    return gateway_policy(data_dir)


def write_account_policy(data_dir: Path, *, tenant_id: str, user_id: str, changes: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Partial update of ONE account: enabled_folders ⊆ allowed_folders; own_folders only while any folder is allowed."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    key = account_key(tenant_id, user_id)
    changes = dict(changes or {})
    changes.pop("ok", None)
    changes.pop("account", None)
    _refuse_unknown(changes, ACCOUNT_FIELDS, what="account workspace policy")
    ensure_migrated(data_dir)
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        g = _stored_gateway(stored)
        accounts = _stored_accounts(stored)
        entry = accounts.get(key) or {"enabled_folders": [], "own_folders": []}
        never_paths = [Path(p) for p in g["never_allowed"]]
        builtin = [Path(p) for p in builtin_never_allowed(data_dir)]
        if "enabled_folders" in changes:
            raw = changes["enabled_folders"]
            if raw is None:
                raw = []
            if not isinstance(raw, list):
                raise WorkspacePolicyError("enabled_folders must be a list of folder paths")
            allowed = set(g["allowed_folders"])
            enabled: List[str] = []
            for item in raw:
                text = str(_real(Path(str(item or "").strip()))) if isinstance(item, str) and item.strip() else ""
                if text not in allowed:
                    raise WorkspacePolicyError(
                        f"{str(item)!r} is not one of the gateway's allowed folders; an account can only switch on what "
                        "the admin allows."
                    )
                if text in enabled:
                    raise WorkspacePolicyError(f"{text!r} is listed twice; nothing was saved.")
                enabled.append(text)
            entry["enabled_folders"] = enabled
        if "own_folders" in changes:
            raw = changes["own_folders"]
            own = _check_folder_list(raw if raw is not None else [], what="My folders")
            if own and not g["allow_any_folder"]:
                raise WorkspacePolicyError(
                    "Your own folders need the admin's \"Allow any folder\"; it is off, so only the shared workspace and "
                    "the allowed folders can be used."
                )
            for p in own:
                hit = _under_any(Path(p), never_paths) or _under_any(Path(p), builtin)
                if hit is not None:
                    raise WorkspacePolicyError(f"{p!r} is inside never-allowed {str(hit)!r}; never allowed wins.")
            entry["own_folders"] = [p for p in own if p != g["shared_workspace"]]
        if entry.get("enabled_folders") or entry.get("own_folders"):
            accounts[key] = entry
        else:
            accounts.pop(key, None)
        if accounts:
            stored[ACCOUNTS_KEY] = accounts
        else:
            stored.pop(ACCOUNTS_KEY, None)
        stored["_last_changed_by"] = str(actor)
        stored["_last_changed_at"] = _now()
        rc._write_store(data_dir, stored)
    audit_policy_change("account", actor=actor, changed=sorted(changes), account=key)
    return account_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
