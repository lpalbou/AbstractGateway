"""abstractgateway.workspace_policy -- which folders an account's agents may use (round 9, operator 2026-10-04).

EXACTLY TWO DIMENSIONS (operator, "Round 9 — FINAL workspace wording"):

1. WHAT can be reached — the POSTURE, the only mechanism that opens or closes a folder:
   - ``allowed_only`` ("Deny everything, allow listed workspaces"): everything is denied, then the listed workspaces are
     allowed (a ``deny`` row carves a sub-folder out of an allowed one);
   - ``any_except_denied`` ("Allow everything, refuse listed workspaces"): everything is allowed at ONE default mode
     (``default_mode``, ro or rw), then the listed folders are exceptions (denied, or their own mode).
   Plus the shared workspace, always in, always read & write.
2. HOW — per folder, read-only (``ro``) or read & write (``rw``), granular.

Gateway policy (admin; ``workspace_policy`` in ``<data>/config/runtime_config.json``):
``{shared_workspace, posture, default_mode, folders: [{path, mode: ro|rw|deny}]}``.

Account policy (people, entities, the admin's own; ``account_workspace_policies[tenant:user]``):
narrows only — ``{default_mode: "ro"|None, folders: [{path, mode: ro|deny}]}``. The effective mode
of a path is the lower of the admin's rule and the account's rule for it (deny < ro < rw); within
each list the most specific row wins, and nothing re-opens beneath a deny.

The agent always also has its PRIVATE session folder (in its account's data plane). A row inside
the gateway's data folder (e.g. another conversation folder) counts only for the account whose own
data plane holds it; the host lifts the built-in data-folder deny for exactly that path.

``effective_policy`` is the ONE computation every enforcement point reads (run starts, the host's
tool sandbox, the run workspace browser, the server file routes). The old model (access modes,
per-user allow/deny lists, launch-folder trust, "Any folder (old clients)") is migrated ONCE
(``migrate_store``) and its keys are refused afterwards.
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

GATEWAY_FIELDS = ("shared_workspace", "posture", "default_mode", "folders")
ACCOUNT_FIELDS = ("default_mode", "folders")
POSTURES = ("allowed_only", "any_except_denied")
POSTURE_LABELS = {"allowed_only": "Deny everything, allow listed workspaces", "any_except_denied": "Allow everything, refuse listed workspaces"}
GATEWAY_MODES = ("ro", "rw", "deny")
ACCOUNT_MODES = ("ro", "deny")
_RANK = {"deny": 0, "ro": 1, "rw": 2}

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
# Field names of the intermediate round-9 drafts, refused by name on the policy routes.
_DRAFT_FIELDS = ("allowed_folders", "never_allowed", "allow_any_folder", "launch_folder_trust", "enabled_folders", "own_folders", "other_sessions", "mode")
LEGACY_MOVED_SENTENCE = (
    "moved to the workspace policy: PUT /api/gateway/workspace/policy {shared_workspace, posture, default_mode, "
    "folders: [{path, mode}]} for the gateway, PUT /api/gateway/workspace/policy/{account} {default_mode, folders} "
    "for one account. The access modes, launch-folder trust and \"Any folder (old clients)\" no longer exist"
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


def _credential_folders(data_dir: Path) -> List[Path]:
    data_root = _real(Path(data_dir))
    return [Path(p) for p in builtin_never_allowed(data_dir) if _real(Path(p)) != data_root]


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
    """What a gateway before this version used when nothing was stored (the legacy env, else the
    runtime's repo-root guess, else the working folder). Used ONLY to freeze an existing install."""
    return str(_store_mod()._workspace_root_fallback())


def _check_folder(raw: Any, *, what: str) -> str:
    """One folder with the path-check rules (absolute, existing directory) -> its real path."""
    from .runtime_config import check_workspace_path

    if not isinstance(raw, str):
        raise WorkspacePolicyError(f"{what}: a workspace path (text) is required, got {type(raw).__name__}")
    out = check_workspace_path(raw)
    if not out.get("valid"):
        raise WorkspacePolicyError(f"{what} {str(raw).strip()!r}: {out.get('sentence') or 'not a directory'}")
    return str(_real(Path(out["normalized"])))


def _check_rows(raw: Any, *, what: str, modes: Tuple[str, ...]) -> List[Dict[str, str]]:
    """Written rows: each ``{path, mode}`` with an explicit mode (a row always says its permission)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkspacePolicyError(f"{what} must be a list of rows {{path, mode}}")
    names = {"ro": "\"ro\" (read-only)", "rw": "\"rw\" (read & write)", "deny": "\"deny\" (denied)"}
    allowed_txt = " or ".join(names[m] for m in modes)
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkspacePolicyError(f"{what}: each row is {{path, mode}} with mode {allowed_txt}.")
        unknown = sorted(set(item) - {"path", "mode"})
        if unknown:
            raise WorkspacePolicyError(f"{what}: unknown row field(s) {unknown}; a row is {{path, mode}}.")
        mode = item.get("mode")
        if mode == "rw" and "rw" not in modes:
            raise WorkspacePolicyError(
                f"{what}: {item.get('path')!r} cannot be read & write here — an account narrows the gateway's policy "
                "(read-only or denied), it never raises it."
            )
        if mode not in modes:
            raise WorkspacePolicyError(f"{what}: mode must be {allowed_txt}; got {mode!r}")
        path = _check_folder(item.get("path"), what=f"{what} entry")
        if any(r["path"] == path for r in out):
            raise WorkspacePolicyError(f"{what}: {path!r} is listed twice; nothing was saved.")
        out.append({"path": path, "mode": mode})
    for row in out:
        for other in out:
            if other is not row and other["mode"] == "deny" and _under(Path(row["path"]), Path(other["path"])):
                raise WorkspacePolicyError(
                    f"{what}: {row['path']!r} is inside the refused workspace {other['path']!r}; nothing re-opens under a deny."
                )
    return out


def _strict_bool(raw: Any, *, what: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise WorkspacePolicyError(f"{what} must be true or false (got {raw!r})")


# ---------------------------------------------------------------- stored shapes


def _rows(raw: Any, modes: Tuple[str, ...]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].strip() and item.get("mode") in modes:
            out.append({"path": item["path"], "mode": item["mode"]})
    return out


def fresh_shared_workspace(data_dir: Path) -> Path:
    """The shared workspace of a FRESH gateway: ``<data_dir>/workspace`` (created on first use)."""
    return _real(Path(data_dir)) / "workspace"


def _stored_gateway(stored: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    raw = stored.get(POLICY_KEY)
    raw = raw if isinstance(raw, dict) else {}
    shared = str(raw.get("shared_workspace") or "").strip() or str(fresh_shared_workspace(data_dir))
    posture = raw.get("posture") if raw.get("posture") in POSTURES else "allowed_only"
    return {
        "shared_workspace": str(_real(Path(shared))),
        "posture": posture,
        "default_mode": "ro" if raw.get("default_mode") == "ro" else "rw",
        "folders": _rows(raw.get("folders"), GATEWAY_MODES),
    }


def _stored_accounts(stored: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = stored.get(ACCOUNTS_KEY)
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, entry in raw.items():
        if isinstance(entry, dict):
            out[str(key)] = {
                "default_mode": "ro" if entry.get("default_mode") == "ro" else None,
                "folders": _rows(entry.get("folders"), ACCOUNT_MODES),
            }
    return out


def _empty_entry() -> Dict[str, Any]:
    return {"default_mode": None, "folders": []}


def _read(data_dir: Path) -> Dict[str, Any]:
    ensure_migrated(data_dir)
    ensure_shared_workspace(data_dir)
    return _store_mod()._read_store(Path(data_dir))


_LEGACY_SHARED_ENV = ("ABSTRACTGATEWAY_WORKSPACE_ROOT", "ABSTRACTGATEWAY_WORKSPACE_DIR")


def _store_is_populated(data_dir: Path) -> bool:
    """Does this data folder already hold work (runs, conversations, accounts)? A fresh one does not."""
    base = Path(data_dir)
    if not base.is_dir():
        return False
    for name in ("first_run.json", "audit_log.jsonl"):
        if (base / name).is_file():
            return True
    try:
        for entry in base.iterdir():
            if entry.is_file() and (entry.name.startswith("ledger_") or entry.name.startswith("run_")):
                return True
    except OSError:
        return False
    ws = base / "workspaces"
    if ws.is_dir() and any(ws.iterdir()):
        return True
    users = base / "auth" / "users.json"
    try:
        if users.is_file() and json.loads(users.read_text() or "{}"):
            return True
    except (OSError, ValueError):
        return True  # unreadable registry: treat as populated (never move an existing install)
    return False


def ensure_shared_workspace(data_dir: Path) -> Optional[str]:
    """Settle the shared workspace ONCE when none is stored, never by guessing (parent rule, round 9):

    - the legacy env (``ABSTRACTGATEWAY_WORKSPACE_ROOT`` / ``ABSTRACTGATEWAY_WORKSPACE_DIR``) when set,
      imported once (then ignored);
    - a data folder that already holds work FREEZES its current effective value (what the gateway
      used before this version), so nothing moves for an existing install;
    - a fresh data folder gets ``<data_dir>/workspace``.

    The decision is stored with a record under ``_migrated.shared_workspace_v1``. Returns how it was
    decided ("env" | "frozen" | "fresh"), or None when a shared workspace was already stored."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    raw = rc._read_store(data_dir).get(POLICY_KEY)
    if isinstance(raw, dict) and str(raw.get("shared_workspace") or "").strip():
        return None
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        policy = stored.get(POLICY_KEY) if isinstance(stored.get(POLICY_KEY), dict) else {}
        if str(policy.get("shared_workspace") or "").strip():
            return None
        env_name = next((n for n in _LEGACY_SHARED_ENV if str(os.getenv(n) or "").strip()), None)
        if env_name:
            source, value = "env", str(_real(Path(str(os.getenv(env_name)).strip())))
        elif _store_is_populated(data_dir):
            source, value = "frozen", str(_real(Path(_previous_effective_shared_workspace())))
        else:
            source, value = "fresh", str(fresh_shared_workspace(data_dir))
        stored[POLICY_KEY] = {**policy, "shared_workspace": value}
        migrated = dict(stored.get("_migrated") or {}) if isinstance(stored.get("_migrated"), dict) else {}
        migrated["shared_workspace_v1"] = {"at": _now(), "source": source, "value": value, **({"env": env_name} if env_name else {})}
        stored["_migrated"] = migrated
        stored["_last_changed_by"] = "system:workspace_policy_migration"
        stored["_last_changed_at"] = _now()
        rc._write_store(data_dir, stored)
    audit_policy_change("migration", actor="system:workspace_policy_migration", changed=["shared_workspace"])
    return source


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

    g = _stored_gateway(_read(data_dir), Path(data_dir))
    fresh = fresh_shared_workspace(data_dir)
    if Path(g["shared_workspace"]) == fresh and not fresh.is_dir():
        try:
            fresh.mkdir(parents=True, exist_ok=True)  # created on first use
        except OSError:
            pass
    try:
        max_bytes = int(str(os.getenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", "") or "").strip() or 0)
    except ValueError:
        max_bytes = 0
    g["builtin_never_allowed"] = builtin_never_allowed(data_dir) if resolve_workspace_builtin_deny_enabled(Path(data_dir)) else []
    g["max_attachment_bytes"] = max_bytes if max_bytes > 0 else 25 * 1024 * 1024
    return g


def account_policy(data_dir: Path, *, tenant_id: str, user_id: str) -> Dict[str, Any]:
    key = account_key(tenant_id, user_id)
    entry = _stored_accounts(_read(data_dir)).get(key) or _empty_entry()
    return {"account": key, "default_mode": entry["default_mode"], "folders": entry["folders"]}


def _rule(rows: List[Dict[str, str]], path: Path) -> Optional[str]:
    """The mode of the most specific row containing ``path``; a deny anywhere above it wins."""
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


class _Resolver:
    """The effective mode of any path for one account (deny < ro < rw)."""

    def __init__(self, g: Dict[str, Any], acc: Dict[str, Any], data_dir: Path, tenant: str, user: str) -> None:
        self.g, self.acc, self.data_dir, self.tenant, self.user = g, acc, Path(data_dir), tenant, user
        self.shared = Path(g["shared_workspace"])
        self.any = g["posture"] == "any_except_denied"
        self.default = _min(g["default_mode"], acc["default_mode"] or "rw") if self.any else None
        # Rows inside the data folder count only inside this account's own plane.
        self.rows = [r for r in g["folders"] if self._row_usable(r)]

    def _row_usable(self, row: Dict[str, str]) -> bool:
        p = Path(row["path"])
        if _under(p, self.data_dir) and not in_own_plane(p, self.data_dir, tenant_id=self.tenant, user_id=self.user):
            return False
        return True

    def mode(self, path: Path) -> str:
        if _under(path, self.shared):
            return "rw"
        admin = _rule(self.rows, path)
        if admin is None:
            admin = self.g["default_mode"] if self.any else "deny"
            account_default = self.acc["default_mode"] or "rw"
        else:
            account_default = "rw"
        account = _rule(self.acc["folders"], path) or account_default
        return _min(admin, account)


def _summary(posture: str, default: Optional[str], shared: str, folders: List[Dict[str, str]]) -> str:
    """One line, the same for every viewer: posture label, then the shared workspace, then each folder."""
    head = POSTURE_LABELS[posture] + (f" ({default})" if posture == "any_except_denied" else "")
    parts = [head, "Shared workspace (rw)"]
    for f in folders:
        if f["source"] == "shared":
            continue
        parts.append(f"{f['path']} ({'refused' if f['mode'] == 'deny' else f['mode']})")
    return " · ".join(parts)


def effective_policy(data_dir: Path, *, tenant_id: Optional[str], user_id: Optional[str]) -> Dict[str, Any]:
    """What this account's agents may use, computed server-side (the only input of enforcement)."""
    g = gateway_policy(data_dir)
    key = account_key(tenant_id, user_id)
    tenant, _, user = key.partition(":")
    acc = _stored_accounts(_read(data_dir)).get(key) or _empty_entry()
    res = _Resolver(g, acc, Path(data_dir), tenant, user)
    folders: List[Dict[str, str]] = [{"path": g["shared_workspace"], "mode": "rw", "source": "shared"}]
    seen = {g["shared_workspace"]}
    for row in res.rows:
        if row["path"] in seen:
            continue
        folders.append({"path": row["path"], "mode": res.mode(Path(row["path"])) if row["mode"] != "deny" else "deny", "source": "gateway"})
        seen.add(row["path"])
    for row in acc["folders"]:
        if row["path"] in seen:
            continue
        folders.append({"path": row["path"], "mode": _min(row["mode"], res.mode(Path(row["path"]))), "source": "account"})
        seen.add(row["path"])
    return {
        "account": key,
        "posture": g["posture"],
        "default_mode": res.default,
        "shared_workspace": g["shared_workspace"],
        "folders": folders,
        "summary": _summary(g["posture"], res.default, g["shared_workspace"], folders),
    }


class EffectiveScope(NamedTuple):
    """The effective set for enforcement (Paths, realpath)."""

    posture: str
    default_mode: Optional[str]
    shared: Path
    reach: List[Path]          # ro/rw folders (shared first)
    read_only: List[Path]      # ro folders
    writable: List[Path]       # rw folders (shared first) — exceptions inside read-only roots
    deny: List[Path]           # gateway + account deny rows
    builtin: List[Path]
    plane_allow: List[Path]    # reachable folders inside this account's own data plane
    resolver: Any

    @property
    def any_folder(self) -> bool:
        return self.posture == "any_except_denied"

    def mode(self, path: Path) -> str:
        return self.resolver.mode(_real(path))


def effective_folder_paths(data_dir: Path, *, tenant_id: Optional[str], user_id: Optional[str]) -> EffectiveScope:
    g = gateway_policy(data_dir)
    key = account_key(tenant_id, user_id)
    tenant, _, user = key.partition(":")
    acc = _stored_accounts(_read(data_dir)).get(key) or _empty_entry()
    res = _Resolver(g, acc, Path(data_dir), tenant, user)
    eff = effective_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
    reach = [_real(Path(f["path"])) for f in eff["folders"] if f["mode"] in ("ro", "rw")]
    return EffectiveScope(
        posture=g["posture"],
        default_mode=res.default,
        shared=_real(Path(g["shared_workspace"])),
        reach=reach,
        read_only=[_real(Path(f["path"])) for f in eff["folders"] if f["mode"] == "ro"],
        writable=[_real(Path(f["path"])) for f in eff["folders"] if f["mode"] == "rw"],
        deny=[_real(Path(f["path"])) for f in eff["folders"] if f["mode"] == "deny"],
        builtin=[Path(p) for p in g["builtin_never_allowed"]],
        plane_allow=[p for p in reach if _under(p, Path(data_dir))],
        resolver=res,
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
        old = [k for k in unknown if k in LEGACY_WRITE_KEYS or k in _DRAFT_FIELDS or k == "workspace_access_mode"]
        hint = (
            " Two dimensions only: the posture with its workspace rows {path, mode}, and each workspace's mode. "
            "Separate allowed and never-allowed lists, launch-folder trust, the access modes and \"Any folder (old "
            "clients)\" no longer exist."
            if old
            else ""
        )
        raise WorkspacePolicyError(f"unknown {what} field(s) {unknown}; nothing was saved. Accepted: {list(fields)}.{hint}")


def write_gateway_policy(data_dir: Path, changes: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    """Partial update: named fields replace, unnamed keep. Validated as a whole before anything lands."""
    rc = _store_mod()
    data_dir = Path(data_dir)
    changes = dict(changes or {})
    changes.pop("ok", None)
    for ro in ("builtin_never_allowed", "max_attachment_bytes", "builtin_never_allowed_hidden"):
        changes.pop(ro, None)  # read-only echoes of a GET are tolerated, never stored
    _refuse_unknown(changes, GATEWAY_FIELDS, what="gateway workspace policy")
    ensure_migrated(data_dir)
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        g = _stored_gateway(stored, data_dir)
        if "shared_workspace" in changes:
            raw = changes["shared_workspace"]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                raise WorkspacePolicyError("The shared workspace is required: choose a directory (it cannot be empty).")
            g["shared_workspace"] = _check_folder(raw, what="Shared workspace")
        if "posture" in changes:
            if changes["posture"] not in POSTURES:
                raise WorkspacePolicyError(
                    f"posture must be \"allowed_only\" (Deny everything, allow listed workspaces) or \"any_except_denied\" (Any workspace "
                    f"except denied); got {changes['posture']!r}"
                )
            g["posture"] = changes["posture"]
        if "default_mode" in changes:
            if changes["default_mode"] not in ("ro", "rw"):
                raise WorkspacePolicyError(f"default_mode must be \"ro\" or \"rw\"; got {changes['default_mode']!r}")
            g["default_mode"] = changes["default_mode"]
        if "folders" in changes:
            g["folders"] = _check_rows(changes["folders"], what="Workspaces", modes=GATEWAY_MODES)

        shared = Path(g["shared_workspace"])
        hit = _under_any(shared, [Path(p) for p in builtin_never_allowed(data_dir)])
        if hit is not None and _real(shared) != fresh_shared_workspace(data_dir):
            raise WorkspacePolicyError(
                f"Shared workspace {str(shared)!r} is inside {str(hit)!r} (the gateway's data directory or a credential "
                "directory), which is never a workspace."
            )
        creds = _credential_folders(data_dir)
        for row in g["folders"]:
            p = Path(row["path"])
            if row["mode"] == "deny" and _under(shared, p):
                raise WorkspacePolicyError(
                    f"{row['path']!r} contains the shared workspace {str(shared)!r}, which is always read & write; deny a "
                    "workspace inside it, or move the shared workspace first."
                )
            if _under(p, shared):
                raise WorkspacePolicyError(
                    f"{row['path']!r} is inside the shared workspace, which is always read & write; list workspaces outside it."
                )
            if row["mode"] != "deny" and _under_any(p, creds) is not None:
                raise WorkspacePolicyError(f"{row['path']!r} is a protected credential directory; it is never a workspace.")
        stored[POLICY_KEY] = {k: g[k] for k in GATEWAY_FIELDS}
        stored["_last_changed_by"] = str(actor)
        stored["_last_changed_at"] = _now()
        rc._write_store(data_dir, stored)
    audit_policy_change("gateway", actor=actor, changed=sorted(changes))
    return gateway_policy(data_dir)


def write_account_policy(data_dir: Path, *, tenant_id: str, user_id: str, changes: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Partial update of ONE account. Narrows only: rows are read-only or denied (never read & write),
    and default_mode may only be lowered to read-only (it applies under "Allow everything, refuse listed workspaces")."""
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
        accounts = _stored_accounts(stored)
        entry = accounts.get(key) or _empty_entry()
        if "default_mode" in changes:
            raw = changes["default_mode"]
            if raw == "rw":
                raise WorkspacePolicyError(
                    "An account cannot raise the default to read & write; it follows the gateway's default or lowers it "
                    "to read-only (\"ro\")."
                )
            if raw not in ("ro", None):
                raise WorkspacePolicyError(f"default_mode must be \"ro\" or null (the gateway's); got {raw!r}")
            entry["default_mode"] = raw
        if "folders" in changes:
            entry["folders"] = _check_rows(changes["folders"], what="Workspaces", modes=ACCOUNT_MODES)
        if entry["default_mode"] or entry["folders"]:
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
