"""Admin-gated runtime configuration surface (continuum c1550, operator
directive laurent 2026-07-13 17:28: "the process-manager / backlog-root /
exec-worker posture should be in settings and configurable from continuum,
only by the admin of the gateway").

The knobs were env-only (a launcher restart losing one env blanked the whole
app — c1526/c1530). This module gives them a persisted store with a visible
SOURCE chain: stored > env > default. The SOURCE field is load-bearing —
the incident was invisible precedence; a UI must be able to say WHICH rung
won.

Persistence: <data_dir>/config/runtime_config.json — survives restarts, so
the launcher env becomes the FALLBACK and the operator's stored choice wins.
Writes are the gateway's alone (the route is admin-gated); this module is
pure read/merge/write with no auth opinion (the route enforces the
principal, the tool_policy precedent).

EXECUTOR REGISTRY (operator ruling 2026-07-14 21:09): FOUR execution agents —
codex, cursor-agent, claude, and abstractcode (local, in-house). The three
externals require installation; the gateway's role is never to install them,
only to PROBE what the host actually serves and surface it so continuum's
requests can be complied with. The registry is a declared table (id, display,
availability probe, aliases) so a new executor slots in without a route
change.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# The env names each knob reads today (the FALLBACK rung). Kept as the
# single source so the resolver and any future reader agree.
_ENV_PROCESS_MANAGER = "ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER"
_ENV_TRIAGE_ROOT = "ABSTRACTGATEWAY_TRIAGE_REPO_ROOT"
_ENV_EXEC_RUNNER = "ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER"
_ENV_EXECUTOR = "ABSTRACTGATEWAY_BACKLOG_EXECUTOR"
_ENV_WORKSPACE_ROOT = "ABSTRACTGATEWAY_WORKSPACE_DIR"
_ENV_WORKSPACE_MOUNTS = "ABSTRACTGATEWAY_WORKSPACE_MOUNTS"
_ENV_ALLOW_CLIENT_WORKSPACE_SCOPE = "ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE"
_ENV_TRUST_CLIENT_WORKSPACE_SCOPE = "ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE"
# Stop kill switch (stop_kill_switch.py): seconds a cancelled run's model call
# may keep running before that inference is killed in process (0 = disabled,
# logged at ERROR at every Stop). Never a process kill.
_ENV_STOP_KILL_SWITCH_S = "ABSTRACTGATEWAY_STOP_KILL_SWITCH_S"
_DEFAULT_STOP_KILL_SWITCH_S = 10.0

_DEFAULT_EXECUTOR = "codex"
_WORKSPACE_MOUNT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")

# Declared executor registry (continuum c1550 ask 2; the four ruled agents
# 2026-07-14). `probe` names an availability check evaluated at read time —
# availability is PROBED (binary/package actually present), never
# configured-only, so the roster tells the operator what will really run.
# `aliases` fold historical/env spellings onto the canonical id (codex_cli
# was the wire value continuum's setup card taught — it must keep working).
_EXECUTOR_REGISTRY: List[Dict[str, Any]] = [
    {"id": "codex", "display": "Codex CLI", "probe": ("bin", "codex"),
     "aliases": ("codex_cli", "codex-cli")},
    {"id": "claude", "display": "Claude Code", "probe": ("bin", "claude"),
     "aliases": ("claude-code", "claude_code", "claude_cli", "claude-cli")},
    {"id": "cursor-agent", "display": "Cursor Agent", "probe": ("bin", "cursor-agent"),
     "aliases": ("cursor", "cursor_agent", "cursoragent")},
    {"id": "abstractcode", "display": "AbstractCode (framework-native)", "probe": ("bin", "abstractcode"),
     "aliases": ("abstract-code", "abstract_code")},
]

_VALID_EXECUTOR_IDS = {e["id"] for e in _EXECUTOR_REGISTRY}


def canonical_executor_id(raw: Any) -> Optional[str]:
    """Fold any accepted spelling onto the canonical executor id; None for
    unknown/empty. ONE folding rule — routes, the runner factory, and the
    settings write all call this, so a spelling accepted anywhere is
    accepted everywhere."""
    s = str(raw or "").strip().lower()
    if not s or s == "none":
        return None
    for entry in _EXECUTOR_REGISTRY:
        if s == entry["id"] or s in entry.get("aliases", ()):
            return str(entry["id"])
    return None


def _bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return default
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _flag_enabled(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "on"}


def _workspace_root_fallback() -> str:
    raw = str(os.getenv(_ENV_WORKSPACE_ROOT, "") or "").strip()
    if raw:
        base = Path(raw).expanduser()
    else:
        # Match the gateway route default: a stable repo root when available,
        # else the process cwd.
        try:
            from abstractruntime.integrations.abstractcore.workspace_scoped_tools import resolve_workspace_base_dir

            base = resolve_workspace_base_dir()
        except Exception:
            base = Path.cwd()
    try:
        return str(base.resolve())
    except Exception:
        return str(base)


def _normalize_workspace_root_display(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return str(Path(text).expanduser().resolve())
    except Exception:
        return str(Path(text).expanduser())


def _parse_workspace_mount_lines(raw: str, *, strict: bool) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for ln in str(raw or "").splitlines():
        line = str(ln or "").strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if strict:
                raise RuntimeConfigError(
                    f"workspace_mounts line {line!r} is invalid — expected name=/abs/path"
                )
            continue
        name, path = line.split("=", 1)
        out.append((name.strip(), path.strip()))
    return out


def _normalize_workspace_mount_entries(raw: Any, *, strict: bool = True) -> list[Dict[str, str]]:
    if raw is None:
        return []
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception as e:
                if strict:
                    raise RuntimeConfigError(
                        f"workspace_mounts must be newline-separated name=/abs/path or a JSON array of objects: {e}"
                    ) from e
                return []
            raw = parsed
        else:
            pairs = _parse_workspace_mount_lines(text, strict=strict)
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                if strict:
                    raise RuntimeConfigError(
                        "workspace_mounts JSON entries must be objects with name and path"
                    )
                continue
            pairs.append((str(item.get("name") or "").strip(), str(item.get("path") or "").strip()))
    elif not pairs:
        if strict:
            raise RuntimeConfigError(
                "workspace_mounts must be newline-separated name=/abs/path or a JSON array of objects"
            )
        return []

    out: list[Dict[str, str]] = []
    seen: set[str] = set()
    for name, path in pairs:
        if not name or not _WORKSPACE_MOUNT_NAME_RE.match(name):
            if strict:
                raise RuntimeConfigError(
                    f"workspace_mounts name {name!r} is invalid — use 1-32 letters, digits, _ or -"
                )
            continue
        if name in seen:
            if strict:
                raise RuntimeConfigError(f"workspace_mounts repeats the name {name!r}")
            continue
        if not path:
            if strict:
                raise RuntimeConfigError(f"workspace_mounts entry {name!r} is missing a path")
            continue
        p = Path(path).expanduser()
        if not p.is_absolute():
            if strict:
                raise RuntimeConfigError(
                    f"workspace_mounts path for {name!r} must be absolute (got {path!r})"
                )
            continue
        try:
            resolved = p.resolve()
        except Exception as e:
            if strict:
                raise RuntimeConfigError(
                    f"workspace_mounts path for {name!r} is invalid ({path!r}: {e})"
                ) from e
            continue
        if not resolved.exists():
            if strict:
                raise RuntimeConfigError(
                    f"workspace_mounts path for {name!r} does not exist ({str(resolved)!r})"
                )
            continue
        if not resolved.is_dir():
            if strict:
                raise RuntimeConfigError(
                    f"workspace_mounts path for {name!r} is not a directory ({str(resolved)!r})"
                )
            continue
        out.append({"name": name, "path": str(resolved)})
        seen.add(name)
    return out


def _format_workspace_mount_entries(entries: list[Dict[str, str]]) -> str:
    lines = []
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        path = str(entry.get("path") or "").strip()
        if name and path:
            lines.append(f"{name}={path}")
    return "\n".join(lines)


def _normalize_workspace_path_list(
    raw: Any,
    *,
    strict: bool = True,
    field_name: str,
) -> list[str]:
    if raw is None:
        return []
    items: list[str] = []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception as e:
                if strict:
                    raise RuntimeConfigError(
                        f"{field_name} must be newline-separated absolute paths or a JSON array of strings: {e}"
                    ) from e
                return []
            raw = parsed
        else:
            items = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            elif strict:
                raise RuntimeConfigError(
                    f"{field_name} must be newline-separated absolute paths or a JSON array of strings"
                )
    elif not items:
        if strict:
            raise RuntimeConfigError(
                f"{field_name} must be newline-separated absolute paths or a JSON array of strings"
            )
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        p = Path(item).expanduser()
        if not p.is_absolute():
            if strict:
                raise RuntimeConfigError(
                    f"{field_name} entries must be absolute directories (got {item!r})"
                )
            continue
        try:
            resolved = p.resolve()
        except Exception as e:
            if strict:
                raise RuntimeConfigError(
                    f"{field_name} entry {item!r} is invalid ({e})"
                ) from e
            continue
        if not resolved.exists():
            if strict:
                raise RuntimeConfigError(
                    f"{field_name} entry {str(resolved)!r} does not exist"
                )
            continue
        if not resolved.is_dir():
            if strict:
                raise RuntimeConfigError(
                    f"{field_name} entry {str(resolved)!r} is not a directory"
                )
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _format_workspace_path_list(entries: list[str]) -> str:
    return "\n".join(str(item).strip() for item in entries if str(item).strip())


# Per-user policy entries accept exactly these fields; anything else is a
# typo the write must refuse rather than silently store-and-ignore.
_USER_POLICY_FIELDS = (
    "mode",
    "workspace_allowed_paths",
    "workspace_blocked_paths",
    "trust_client_launch_folder",
    "client_workspace_scope_overrides",
)

# Per-user access posture (operator clarification 2026-08-19): each user
# chooses how their agents' filesystem scope is decided —
#   whitelist (default): deny everything, allow the configured roots (plus
#     the launch folder when trust is on);
#   blacklist: allow everything, refuse the blocked roots (the gateway-wide
#     deny list still always applies).
_USER_POLICY_MODES = ("whitelist", "blacklist")


def _normalize_user_policy_key(raw: Any, *, strict: bool = True) -> Optional[str]:
    """Fold a per-user policy key onto the registry's canonical
    "tenant:user" shape (users.py GatewayUserRecord.key). A bare "user"
    means the default tenant — the single-operator spelling."""
    text = str(raw or "").strip()
    if not text:
        if strict:
            raise RuntimeConfigError("user_workspace_policies keys must be 'tenant:user' or 'user'")
        return None
    if ":" in text:
        tenant, _, user = text.partition(":")
    else:
        tenant, user = "default", text
    tenant = tenant.strip()
    user = user.strip()
    if not tenant or not user:
        if strict:
            raise RuntimeConfigError(
                f"user_workspace_policies key {text!r} is invalid — use 'tenant:user' or 'user'"
            )
        return None
    return f"{tenant}:{user}"


def _optional_bool(raw: Any, *, field: str, strict: bool = True) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s or s in {"inherit", "none", "null"}:
        return None
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    if strict:
        raise RuntimeConfigError(f"{field} must be true, false, or omitted (inherit); got {raw!r}")
    return None


def _normalize_user_workspace_policies(raw: Any, *, strict: bool = True) -> Dict[str, Dict[str, Any]]:
    """Validate the per-user policy map. Accepts a dict or a JSON-object
    string (console textareas send text). Every entry is validated BEFORE
    any of it lands (the validate-before-write rule of this store)."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except Exception as e:
            if strict:
                raise RuntimeConfigError(
                    f"user_workspace_policies must be a JSON object keyed by 'tenant:user': {e}"
                ) from e
            return {}
    if not isinstance(raw, dict):
        if strict:
            raise RuntimeConfigError(
                "user_workspace_policies must be an object keyed by 'tenant:user'"
            )
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_entry in raw.items():
        key = _normalize_user_policy_key(raw_key, strict=strict)
        if key is None:
            continue
        if raw_entry is None:
            continue  # explicit null = remove this user's overrides
        if not isinstance(raw_entry, dict):
            if strict:
                raise RuntimeConfigError(
                    f"user_workspace_policies[{key!r}] must be an object with any of: "
                    + ", ".join(_USER_POLICY_FIELDS)
                )
            continue
        unknown = sorted(set(raw_entry) - set(_USER_POLICY_FIELDS))
        if unknown and strict:
            raise RuntimeConfigError(
                f"user_workspace_policies[{key!r}] has unknown fields {unknown} — "
                f"accepted: {list(_USER_POLICY_FIELDS)}"
            )
        entry: Dict[str, Any] = {}
        raw_mode = str(raw_entry.get("mode") or "").strip().lower()
        if raw_mode:
            if raw_mode not in _USER_POLICY_MODES:
                if strict:
                    raise RuntimeConfigError(
                        f"user_workspace_policies[{key!r}].mode must be one of "
                        f"{list(_USER_POLICY_MODES)} (whitelist = deny everything and "
                        f"allow the listed roots; blacklist = allow everything and "
                        f"refuse the listed roots); got {raw_mode!r}"
                    )
            else:
                entry["mode"] = raw_mode
        allowed = _normalize_workspace_path_list(
            raw_entry.get("workspace_allowed_paths"),
            strict=strict,
            field_name=f"user_workspace_policies[{key!r}].workspace_allowed_paths",
        )
        if allowed:
            entry["workspace_allowed_paths"] = allowed
        blocked = _normalize_workspace_path_list(
            raw_entry.get("workspace_blocked_paths"),
            strict=strict,
            field_name=f"user_workspace_policies[{key!r}].workspace_blocked_paths",
        )
        if blocked:
            entry["workspace_blocked_paths"] = blocked
        trust = _optional_bool(
            raw_entry.get("trust_client_launch_folder"),
            field=f"user_workspace_policies[{key!r}].trust_client_launch_folder",
            strict=strict,
        )
        if trust is not None:
            entry["trust_client_launch_folder"] = trust
        overrides = _optional_bool(
            raw_entry.get("client_workspace_scope_overrides"),
            field=f"user_workspace_policies[{key!r}].client_workspace_scope_overrides",
            strict=strict,
        )
        if overrides is not None:
            entry["client_workspace_scope_overrides"] = overrides
        if entry:
            out[key] = entry
    return out


def _workspace_root_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    if "workspace_root" in stored and stored["workspace_root"] is not None:
        return {
            "value": _normalize_workspace_root_display(stored.get("workspace_root")),
            "source": "stored",
        }
    env_raw = os.getenv(_ENV_WORKSPACE_ROOT)
    if env_raw is not None and str(env_raw).strip():
        return {
            "value": _normalize_workspace_root_display(env_raw),
            "source": "env",
        }
    return {"value": _workspace_root_fallback(), "source": "default"}


def _workspace_mounts_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    if "workspace_mounts" in stored and stored["workspace_mounts"] is not None:
        entries = _normalize_workspace_mount_entries(stored.get("workspace_mounts"), strict=False)
        return {
            "value": _format_workspace_mount_entries(entries),
            "source": "stored",
            "entries": entries,
        }
    env_raw = os.getenv(_ENV_WORKSPACE_MOUNTS)
    if env_raw is not None and str(env_raw).strip():
        entries = _normalize_workspace_mount_entries(str(env_raw), strict=False)
        return {
            "value": _format_workspace_mount_entries(entries),
            "source": "env",
            "entries": entries,
        }
    return {"value": "", "source": "default", "entries": []}


def _workspace_allowed_paths_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    mounts = _workspace_mounts_payload(stored)
    entries = mounts.get("entries") or []
    paths = [str(entry.get("path") or "").strip() for entry in entries if isinstance(entry, dict) and str(entry.get("path") or "").strip()]
    return {
        "value": _format_workspace_path_list(paths),
        "source": str(mounts.get("source") or "default"),
        "paths": paths,
    }


def _workspace_blocked_paths_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    if "workspace_blocked_paths" in stored and stored["workspace_blocked_paths"] is not None:
        paths = _normalize_workspace_path_list(
            stored.get("workspace_blocked_paths"),
            strict=False,
            field_name="workspace_blocked_paths",
        )
        return {
            "value": _format_workspace_path_list(paths),
            "source": "stored",
            "paths": paths,
        }
    return {"value": "", "source": "default", "paths": []}


def _client_workspace_scope_overrides_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    if "client_workspace_scope_overrides" in stored and stored["client_workspace_scope_overrides"] is not None:
        return {
            "value": _bool(stored["client_workspace_scope_overrides"], False),
            "source": "stored",
        }
    if _flag_enabled(os.getenv(_ENV_ALLOW_CLIENT_WORKSPACE_SCOPE)):
        return {"value": True, "source": "env"}
    if _flag_enabled(os.getenv(_ENV_TRUST_CLIENT_WORKSPACE_SCOPE)):
        return {"value": True, "source": "env"}
    tool_mode = str(os.getenv("ABSTRACTGATEWAY_TOOL_MODE") or "").strip().lower()
    return {"value": tool_mode == "local", "source": "default"}


def _workspace_default_mode_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    """The GATEWAY default posture — what every principal without their own
    stored mode inherits (operator order 2026-08-19: "we are the gateway,
    where do we set the default"). whitelist = deny everything, allow the
    configured roots (the shipped default); blacklist = allow everything,
    refuse the deny lists. Stored > default; no env rung."""
    raw = str(stored.get("workspace_default_mode") or "").strip().lower()
    if raw in _USER_POLICY_MODES:
        return {"value": raw, "source": "stored"}
    return {"value": "whitelist", "source": "default"}


def _trust_client_launch_folder_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    """Launch-folder trust: a run started from a client folder may use that
    folder as its writable workspace_root. Operator ruling 2026-08-19: the
    DEFAULT is TRUE — wherever an agent is started, it can write in the
    folder it starts — and the knob is a SETTING, deliberately with NO env
    rung (stored > default only; the env era for this class is over)."""
    if "trust_client_launch_folder" in stored and stored["trust_client_launch_folder"] is not None:
        return {"value": _bool(stored["trust_client_launch_folder"], True), "source": "stored"}
    return {"value": True, "source": "default"}


def _user_workspace_policies_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    if "user_workspace_policies" in stored and stored["user_workspace_policies"] is not None:
        policies = _normalize_user_workspace_policies(stored.get("user_workspace_policies"), strict=False)
        return {"value": policies, "source": "stored", "count": len(policies)}
    return {"value": {}, "source": "default", "count": 0}


def _store_path(data_dir: Path) -> Path:
    return Path(data_dir) / "config" / "runtime_config.json"


class RuntimeConfigStoreCorrupt(RuntimeError):
    """The settings store file exists but is unparseable. Raised on the WRITE
    path so a save never read-mutate-replaces a corrupt file into oblivion."""


def _read_store(data_dir: Path, *, strict: bool = False) -> Dict[str, Any]:
    """The persisted operator choices, `stored > env > default`.

    READ path (strict=False): a missing file is empty; a CORRUPT file
    degrades to empty (env/default take over) but LOUDLY — a silent {} used
    to hide a store the operator thinks is live. WRITE path (strict=True):
    a corrupt file RAISES `RuntimeConfigStoreCorrupt` — critical because
    write_runtime_config does read-mutate-replace, so mutating a corrupt-read
    {} and writing it back would WIPE every other stored knob (harmless at
    4 knobs, catastrophic as the env-var migration grows the store to ~100;
    backlog 0063/env-kill design adversary P0)."""
    path = _store_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        if strict:
            raise RuntimeConfigStoreCorrupt(
                f"runtime config store is unreadable ({path}: {type(e).__name__}: {e}); "
                "refusing to write over it — inspect/repair the file, then retry"
            ) from e
        import logging

        logging.getLogger("abstractgateway.runtime_config").warning(
            "#FALLBACK runtime config store unreadable (%s: %s) — running on env/defaults; "
            "stored operator choices are NOT being applied until the file is repaired",
            path,
            e,
        )
        return {}
    return data if isinstance(data, dict) else {}


def _resolve(stored: Dict[str, Any], key: str, env_name: str, default: Any, *, as_bool: bool = False) -> Dict[str, Any]:
    """One knob's {value, source}: stored > env > default.

    RULED LAW (operator dm#194, 2026-07-21): the console/CLI-set stored value
    SUPERSEDES env ALWAYS — env is a labeled fallback BELOW config, never
    above (the two carve-outs, boot-pointer + security-inversion, are
    classified out at the registry level and never reach this resolver). The
    source names the winning rung so a launcher losing an env is VISIBLE,
    never silent, and a shadowed env is diagnosable."""
    if key in stored and stored[key] is not None:
        value = _bool(stored[key], bool(default)) if as_bool else stored[key]
        return {"value": value, "source": "stored"}
    env_raw = os.getenv(env_name)
    if env_raw is not None and str(env_raw).strip():
        value = _bool(env_raw, bool(default)) if as_bool else str(env_raw).strip()
        return {"value": value, "source": "env"}
    return {"value": default, "source": "default"}


def executor_available(entry: Dict[str, Any]) -> bool:
    """Probe an executor's real availability (binary on PATH / package
    importable). A probe failure reads as unavailable — the roster never
    claims an executor that would fail at run time."""
    probe = entry.get("probe")
    if not isinstance(probe, tuple) or len(probe) != 2:
        return False
    kind, target = probe
    try:
        if kind == "bin":
            return shutil.which(str(target)) is not None
        if kind == "py":
            import importlib.util

            return importlib.util.find_spec(str(target)) is not None
    except Exception:
        return False
    return False


def executor_registry() -> List[Dict[str, Any]]:
    """The served executor roster (continuum renders it as the agent
    roster). `available` is probed fresh each call; `default` marks codex."""
    out: List[Dict[str, Any]] = []
    for entry in _EXECUTOR_REGISTRY:
        out.append({
            "id": entry["id"],
            "display": entry["display"],
            "available": executor_available(entry),
            "default": entry["id"] == _DEFAULT_EXECUTOR,
        })
    return out


def read_runtime_config(data_dir: Path, *, is_admin: bool = True) -> Dict[str, Any]:
    """The authoritative runtime-config posture (continuum c1550 ask 1).
    Each knob carries {value, source}; the executor also carries the
    registry so one GET renders the whole Settings pane.

    `writable` (continuum c1563 amendment, routed by agency c1566): the
    caller's principal-resolved write authority, so a console never renders
    write controls that always 403 for non-admins. `is_admin=False` ALSO
    redacts the triage_repo_root PATH VALUE — a non-admin sees whether it is
    configured (`configured: bool` + source) but not the absolute server
    path (the path-leak class I flagged in the production audit; closed here
    for this surface). POST stays admin-gated regardless — this only widens
    the read to a labeled read-only posture."""
    stored = _read_store(data_dir)
    triage = _resolve(stored, "triage_repo_root", _ENV_TRIAGE_ROOT, None)
    workspace_root = _workspace_root_payload(stored)
    workspace_mounts = _workspace_mounts_payload(stored)
    workspace_allowed_paths = _workspace_allowed_paths_payload(stored)
    workspace_blocked_paths = _workspace_blocked_paths_payload(stored)
    user_workspace_policies = _user_workspace_policies_payload(stored)
    if not is_admin:
        # Redact the path; keep the posture (configured + which rung won).
        triage = {"configured": bool(triage.get("value")), "source": triage["source"]}
        workspace_root = {"configured": bool(workspace_root.get("value")), "source": workspace_root["source"]}
        workspace_mounts = {"configured": bool((workspace_mounts.get("entries") or [])), "source": workspace_mounts["source"]}
        workspace_allowed_paths = {"configured": bool((workspace_allowed_paths.get("paths") or [])), "source": workspace_allowed_paths["source"]}
        workspace_blocked_paths = {"configured": bool((workspace_blocked_paths.get("paths") or [])), "source": workspace_blocked_paths["source"]}
        # Per-user policies name other users AND server paths — non-admins
        # see only that some exist (same redaction discipline as above).
        user_workspace_policies = {
            "configured": bool(user_workspace_policies.get("value")),
            "count": int(user_workspace_policies.get("count") or 0),
            "source": user_workspace_policies["source"],
        }
    operator_email = resolve_operator_email(data_dir, stored=stored)
    if not is_admin:
        # An address is PII-adjacent — non-admins see the posture only
        # (same redaction discipline as triage_repo_root).
        operator_email = {"configured": bool(operator_email.get("value")), "source": operator_email["source"]}
    return {
        "writable": bool(is_admin),
        "process_manager": _resolve(stored, "process_manager", _ENV_PROCESS_MANAGER, False, as_bool=True),
        "triage_repo_root": triage,
        "workspace_root": workspace_root,
        "workspace_mounts": workspace_mounts,
        "workspace_allowed_paths": workspace_allowed_paths,
        "workspace_blocked_paths": workspace_blocked_paths,
        "client_workspace_scope_overrides": _client_workspace_scope_overrides_payload(stored),
        "trust_client_launch_folder": _trust_client_launch_folder_payload(stored),
        "workspace_default_mode": _workspace_default_mode_payload(stored),
        "user_workspace_policies": user_workspace_policies,
        "backlog_exec_runner": _resolve(stored, "backlog_exec_runner", _ENV_EXEC_RUNNER, False, as_bool=True),
        "executor": _resolve(stored, "executor", _ENV_EXECUTOR, _DEFAULT_EXECUTOR),
        "executors": executor_registry(),
        "operator_email": operator_email,
        "stop_kill_switch_s": _stop_kill_switch_seconds_payload(stored),
    }


def _parse_kill_switch_seconds(raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError("a boolean is not a number of seconds")
    value = float(raw)
    if value != value or value < 0 or value == float("inf"):
        raise ValueError("must be a finite number >= 0 (0 disables the kill switch)")
    return value


def _stop_kill_switch_seconds_payload(stored: Dict[str, Any], default: float = _DEFAULT_STOP_KILL_SWITCH_S) -> Dict[str, Any]:
    """{value, source} for the Stop kill-switch deadline. An UNPARSEABLE env
    value is not silently replaced: the default applies and `invalid_env`
    names what was rejected."""
    resolved = _resolve(stored, "stop_kill_switch_s", _ENV_STOP_KILL_SWITCH_S, default)
    try:
        resolved["value"] = _parse_kill_switch_seconds(resolved["value"])
    except (TypeError, ValueError) as exc:
        return {"value": float(default), "source": "default", "invalid_" + resolved["source"]: f"{resolved['value']!r}: {exc}"}
    return resolved


def resolve_operator_email(
    data_dir: Path,
    *,
    stored: Optional[Dict[str, Any]] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The REGISTERED USER EMAIL (laurent's send_email modulation c4677 +
    the dm#246 order via c4693): the one address the recipient refiner
    treats as "self", also the notification target when configured.

    ONE SOURCE OF TRUTH per the order: when the caller names a principal
    (tenant_id+user_id) and that ACCOUNT RECORD exists, the record decides —
    its email (source "account"), or None when the account has none set
    (feature simply OFF; no fallback wandering past an existing record).
    The stored settings knob serves only the ACCOUNT-LESS posture
    (static-token single-user gateways with no user registry record).
    NEVER ENV (dm#246): the short-lived ABSTRACTGATEWAY_OPERATOR_EMAIL env
    rung and the email-bridge seed were removed same-day they were added —
    email is registered through the account surface or the config store,
    nowhere else. Normalized lowercase; the refiner compares
    case-insensitive EXACT (aliases/plus-tags are NOT self — c4678 pin)."""
    if user_id:
        try:
            from .users import GatewayUserRegistry

            rec = GatewayUserRegistry().get_user(str(user_id), tenant_id=str(tenant_id or "default"))
            if rec is not None:
                email = str(getattr(rec, "email", "") or "").strip().lower()
                if email:
                    return {"value": email, "source": "account"}
                # Account exists WITHOUT an email. For NON-admin principals
                # this is OFF — falling to the gateway knob would leak the
                # ADMIN's address as "self" into another user's runs
                # (cross-account, the exact widening the exact-match rule
                # exists to prevent). The ADMIN/default principal is the
                # single-user operator the knob serves — an email-less admin
                # record is an older record, not a deliberate OFF statement
                # (email-adversary P2: the knob's own audience was silently
                # disabled by the record's mere existence).
                if str(user_id) != "admin" or str(tenant_id or "default") != "default":
                    return {"value": None, "source": "account"}
        except Exception:  # noqa: BLE001 - registry unreadable: fall through to the knob
            pass
    if stored is None:
        stored = _read_store(data_dir)
    value = stored.get("operator_email")
    if isinstance(value, str) and value.strip():
        return {"value": value.strip().lower(), "source": "stored"}
    return {"value": None, "source": "default"}


class RuntimeConfigError(ValueError):
    """A rejected config write — the route maps it to an operator-readable 4xx."""


def write_runtime_config(data_dir: Path, changes: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    """Persist a PARTIAL update (only the named knobs change; unnamed knobs
    keep their stored value or fall through to env/default). Validates each
    field BEFORE writing — a rejected value never lands. Returns the fresh
    posture (the same shape read_runtime_config serves) plus the actor who
    changed it (principal-stamped in the response, the state-marker precedent).

    A corrupt store REFUSES the write (RuntimeConfigStoreCorrupt → the route
    maps it to a 409) instead of wiping the other knobs — a partial update
    must never destroy choices it did not name."""
    stored = _read_store(data_dir, strict=True)
    applied: Dict[str, Any] = {}

    if "process_manager" in changes:
        stored["process_manager"] = _bool(changes["process_manager"], False)
        applied["process_manager"] = stored["process_manager"]
    if "backlog_exec_runner" in changes:
        stored["backlog_exec_runner"] = _bool(changes["backlog_exec_runner"], False)
        applied["backlog_exec_runner"] = stored["backlog_exec_runner"]
    if "desktop_tray" in changes:
        # RETIRED (operator ruling 2026-09-06): the menu bar / tray icon is the
        # gateway's presence on the desktop, so while it serves, it is there.
        # An unknown key is otherwise ignored silently; a caller still sending
        # this one is acting on a setting that no longer exists and deserves to
        # be told, not to get a 200 that changed nothing.
        raise RuntimeConfigError(
            "desktop_tray was removed: the menu bar / tray icon is always shown while the gateway "
            "runs. It can only be absent when this machine cannot hold it (no desktop session, "
            "`serve --reload`, a runner-only process, or the `tray` extra not installed) — "
            "GET /api/gateway/host/tray names which."
        )
    if "triage_repo_root" in changes:
        raw = changes["triage_repo_root"]
        if raw is None or str(raw).strip() == "":
            stored.pop("triage_repo_root", None)  # clear = fall back to env/default
            applied["triage_repo_root"] = None
        else:
            path = Path(str(raw)).expanduser()
            if not path.is_dir():
                raise RuntimeConfigError(
                    f"triage_repo_root {str(raw)!r} is not an existing directory — "
                    "the backlog surface reads docs/backlog under it"
                )
            stored["triage_repo_root"] = str(path.resolve())
            applied["triage_repo_root"] = stored["triage_repo_root"]
    if "workspace_root" in changes:
        raw = changes["workspace_root"]
        if raw is None or str(raw).strip() == "":
            stored.pop("workspace_root", None)
            applied["workspace_root"] = None
        else:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                raise RuntimeConfigError(
                    f"workspace_root must be an absolute directory (got {str(raw)!r})"
                )
            if not path.is_dir():
                raise RuntimeConfigError(
                    f"workspace_root {str(raw)!r} is not an existing directory"
                )
            stored["workspace_root"] = str(path.resolve())
            applied["workspace_root"] = stored["workspace_root"]
    if "workspace_mounts" in changes:
        raw_mounts = changes["workspace_mounts"]
        if raw_mounts is None or (
            isinstance(raw_mounts, str) and not raw_mounts.strip()
        ) or (isinstance(raw_mounts, list) and not raw_mounts):
            stored.pop("workspace_mounts", None)
            applied["workspace_mounts"] = []
        else:
            entries = _normalize_workspace_mount_entries(raw_mounts, strict=True)
            stored["workspace_mounts"] = entries
            applied["workspace_mounts"] = list(entries)
    if "workspace_allowed_paths" in changes:
        raw_allowed = changes["workspace_allowed_paths"]
        if raw_allowed is None or (
            isinstance(raw_allowed, str) and not raw_allowed.strip()
        ) or (isinstance(raw_allowed, list) and not raw_allowed):
            stored.pop("workspace_mounts", None)
            applied["workspace_allowed_paths"] = []
        else:
            paths = _normalize_workspace_path_list(
                raw_allowed,
                strict=True,
                field_name="workspace_allowed_paths",
            )
            from abstractruntime.utils.workspace_paths import build_workspace_mounts

            named = build_workspace_mounts(
                allowed_dirs=[Path(path) for path in paths],
                used_names=set(),
            )
            entries = [{"name": name, "path": str(path)} for name, path in named.items()]
            stored["workspace_mounts"] = entries
            applied["workspace_allowed_paths"] = list(paths)
    if "workspace_blocked_paths" in changes:
        raw_blocked = changes["workspace_blocked_paths"]
        if raw_blocked is None or (
            isinstance(raw_blocked, str) and not raw_blocked.strip()
        ) or (isinstance(raw_blocked, list) and not raw_blocked):
            stored.pop("workspace_blocked_paths", None)
            applied["workspace_blocked_paths"] = []
        else:
            paths = _normalize_workspace_path_list(
                raw_blocked,
                strict=True,
                field_name="workspace_blocked_paths",
            )
            stored["workspace_blocked_paths"] = list(paths)
            applied["workspace_blocked_paths"] = list(paths)
    if "client_workspace_scope_overrides" in changes:
        stored["client_workspace_scope_overrides"] = _bool(
            changes["client_workspace_scope_overrides"], False
        )
        applied["client_workspace_scope_overrides"] = stored["client_workspace_scope_overrides"]
    if "trust_client_launch_folder" in changes:
        raw_trust = changes["trust_client_launch_folder"]
        if raw_trust is None or (isinstance(raw_trust, str) and not raw_trust.strip()):
            stored.pop("trust_client_launch_folder", None)  # clear = default (True)
            applied["trust_client_launch_folder"] = None
        else:
            stored["trust_client_launch_folder"] = _bool(raw_trust, True)
            applied["trust_client_launch_folder"] = stored["trust_client_launch_folder"]
    if "workspace_default_mode" in changes:
        raw_mode = changes["workspace_default_mode"]
        if raw_mode is None or (isinstance(raw_mode, str) and not raw_mode.strip()):
            stored.pop("workspace_default_mode", None)  # clear = whitelist default
            applied["workspace_default_mode"] = None
        else:
            mode = str(raw_mode).strip().lower()
            if mode not in _USER_POLICY_MODES:
                raise RuntimeConfigError(
                    f"workspace_default_mode must be one of {list(_USER_POLICY_MODES)}; got {raw_mode!r}"
                )
            stored["workspace_default_mode"] = mode
            applied["workspace_default_mode"] = mode
    if "user_workspace_policies" in changes:
        raw_policies = changes["user_workspace_policies"]
        policies = _normalize_user_workspace_policies(raw_policies, strict=True)
        if policies:
            stored["user_workspace_policies"] = policies
        else:
            stored.pop("user_workspace_policies", None)
        applied["user_workspace_policies"] = policies
    if "executor" in changes:
        raw_exec = str(changes["executor"] or "").strip()
        exec_id = canonical_executor_id(raw_exec)
        if exec_id is None:
            raise RuntimeConfigError(
                f"unknown executor {raw_exec!r} — the registry offers {sorted(_VALID_EXECUTOR_IDS)}"
            )
        stored["executor"] = exec_id
        applied["executor"] = exec_id
    if "operator_email" in changes:
        raw_email = changes["operator_email"]
        if raw_email is None or str(raw_email).strip() == "":
            stored.pop("operator_email", None)  # clear = fall back to env/bridge
            applied["operator_email"] = None
        else:
            addr = str(raw_email).strip().lower()
            # Single-address shape check only (never full RFC parsing): one
            # @, no separators that smuggle a list. The refiner's exact-match
            # comparison is the real gate; this keeps garbage out of the store.
            if addr.count("@") != 1 or any(c in addr for c in (",", ";", " ", "<", ">")) or addr.startswith("@") or addr.endswith("@"):
                raise RuntimeConfigError(
                    f"operator_email must be ONE plain address (got {str(raw_email)!r}) — "
                    "lists/display-names are not accepted; the refiner treats exactly this address as self"
                )
            stored["operator_email"] = addr
            applied["operator_email"] = addr

    if "stop_kill_switch_s" in changes:
        raw_s = changes["stop_kill_switch_s"]
        if raw_s is None or str(raw_s).strip() == "":
            stored.pop("stop_kill_switch_s", None)  # clear = fall back to env/default
            applied["stop_kill_switch_s"] = None
        else:
            try:
                seconds = _parse_kill_switch_seconds(raw_s)
            except (TypeError, ValueError) as exc:
                raise RuntimeConfigError(f"stop_kill_switch_s {raw_s!r} rejected: {exc}")
            stored["stop_kill_switch_s"] = seconds
            applied["stop_kill_switch_s"] = seconds

    if not applied:
        raise RuntimeConfigError(
            "no recognized config keys in the request (one of: process_manager, "
            "backlog_exec_runner, triage_repo_root, workspace_root, workspace_mounts, "
            "workspace_allowed_paths, workspace_blocked_paths, "
            "client_workspace_scope_overrides, trust_client_launch_folder, "
            "workspace_default_mode, user_workspace_policies, executor, operator_email, "
            "stop_kill_switch_s)"
        )

    stored["_last_changed_by"] = str(actor)
    from datetime import datetime, timezone

    stored["_last_changed_at"] = datetime.now(timezone.utc).isoformat()
    _write_store(data_dir, stored)

    out = read_runtime_config(data_dir)
    out["applied"] = applied
    out["changed_by"] = str(actor)
    return out


def _write_store(data_dir: Path, stored: Dict[str, Any]) -> None:
    path = _store_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the last-good copy before replacing (recovery for a bad write /
    # future corruption): a `.bak` alongside the atomic tmp->replace. The
    # replace stays atomic — a torn config read must never blank the app
    # (the c1526 lesson); the .bak is defense for the file itself.
    if path.exists():
        try:
            path.replace(path.with_suffix(".json.bak"))
        except Exception:
            pass  # backup is best-effort; never block a valid write
    # Unique temp per writer (see provider_endpoint_profiles for the shape of
    # the bug a shared `.json.tmp` allows). Same directory keeps it atomic.
    tmp = path.with_suffix(f".json.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


# ---- Resolvers: the CONSUMER-facing read (value only, stored>env>default) ----
# The surface persists values; these are what the actual knob-readers call so
# a stored choice is HONORED, not cosmetic. Each mirrors read_runtime_config's
# precedence for one knob but returns the bare value.

def resolve_stop_kill_switch(data_dir: Path, *, default_deadline_s: float = _DEFAULT_STOP_KILL_SWITCH_S) -> Dict[str, Any]:
    """{deadline_s, source} for the Stop kill switch (stored > env > default).

    Read at EVERY arm, so a console change applies to the next Stop without a
    restart. Reads only this module's store — never the rest of the posture."""
    stored = _read_store(data_dir)
    seconds = _stop_kill_switch_seconds_payload(stored, default_deadline_s)
    out = {"deadline_s": float(seconds["value"]), "source": seconds["source"]}
    for key, value in seconds.items():
        if key.startswith("invalid_"):
            out[key] = value
    return out


def resolve_process_manager_enabled(data_dir: Path) -> bool:
    return bool(read_runtime_config(data_dir)["process_manager"]["value"])


def resolve_triage_repo_root(data_dir: Path) -> Optional[str]:
    v = read_runtime_config(data_dir)["triage_repo_root"]["value"]
    return str(v) if v else None


def resolve_backlog_exec_runner_enabled(data_dir: Path) -> bool:
    return bool(read_runtime_config(data_dir)["backlog_exec_runner"]["value"])


def resolve_workspace_root(data_dir: Path) -> Path:
    value = read_runtime_config(data_dir)["workspace_root"]["value"]
    text = str(value or "").strip()
    base = Path(text).expanduser() if text else Path.cwd()
    try:
        return base.resolve()
    except Exception:
        return base


def resolve_workspace_mounts(data_dir: Path) -> Dict[str, Path]:
    payload = read_runtime_config(data_dir)["workspace_mounts"]
    out: Dict[str, Path] = {}
    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        path = str(entry.get("path") or "").strip()
        if not name or not path:
            continue
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            continue
        try:
            if not resolved.exists() or not resolved.is_dir():
                continue
        except Exception:
            continue
        out[name] = resolved
    return out


def resolve_workspace_blocked_paths(data_dir: Path) -> tuple[Path, ...]:
    payload = read_runtime_config(data_dir)["workspace_blocked_paths"]
    out: list[Path] = []
    paths = payload.get("paths") or []
    if not isinstance(paths, list):
        return ()
    for item in paths:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            resolved = Path(text).expanduser().resolve()
        except Exception:
            continue
        try:
            if not resolved.exists() or not resolved.is_dir():
                continue
        except Exception:
            continue
        out.append(resolved)
    return tuple(out)


def _stored_user_policy(data_dir: Path, *, tenant_id: Optional[str], user_id: Optional[str]) -> Dict[str, Any]:
    """The validated per-user policy entry for one principal ({} when none)."""
    if not user_id:
        return {}
    key = _normalize_user_policy_key(f"{tenant_id or 'default'}:{user_id}", strict=False)
    if key is None:
        return {}
    stored = _read_store(data_dir)
    policies = _normalize_user_workspace_policies(stored.get("user_workspace_policies"), strict=False)
    entry = policies.get(key)
    return dict(entry) if isinstance(entry, dict) else {}


def resolve_client_workspace_scope_overrides_enabled(
    data_dir: Path,
    *,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    entry = _stored_user_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
    if "client_workspace_scope_overrides" in entry:
        return bool(entry["client_workspace_scope_overrides"])
    return bool(read_runtime_config(data_dir)["client_workspace_scope_overrides"]["value"])


def resolve_trust_client_launch_folder(
    data_dir: Path,
    *,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    """Per-user override wins; otherwise the gateway-wide knob (default True)."""
    entry = _stored_user_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
    if "trust_client_launch_folder" in entry:
        return bool(entry["trust_client_launch_folder"])
    return bool(read_runtime_config(data_dir)["trust_client_launch_folder"]["value"])


def resolve_workspace_default_mode(data_dir: Path) -> str:
    """The gateway default posture (whitelist unless the operator stored
    blacklist) — what a principal without their own mode inherits."""
    return str(read_runtime_config(data_dir)["workspace_default_mode"]["value"])


def resolve_user_workspace_mode(
    data_dir: Path,
    *,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """This principal's access posture: their stored mode, else the GATEWAY
    default mode ("whitelist" = deny everything, allow the configured roots;
    "blacklist" = allow everything, refuse the blocked roots)."""
    entry = _stored_user_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
    mode = str(entry.get("mode") or "").strip().lower()
    if mode in _USER_POLICY_MODES:
        return mode
    return resolve_workspace_default_mode(data_dir)


def read_user_workspace_policy(
    data_dir: Path,
    *,
    tenant_id: Optional[str],
    user_id: Optional[str],
) -> Dict[str, Any]:
    """ONE user's workspace policy, self-service shape: their stored entry
    plus the EFFECTIVE posture after inheritance (what actually governs
    their run starts). Serves GET /workspace/policy/self and the admin
    per-runtime modal.

    The EFFECTIVE trust mirrors enforcement exactly (design adversary B5a):
    the scope-overrides grant IMPLIES launch-folder trust in
    _sanitize_run_workspace_policy, so a UI reading this must never show
    trust "off" while runs behave as on."""
    entry = _stored_user_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
    cfg = read_runtime_config(data_dir)
    trust = entry.get("trust_client_launch_folder")
    if trust is None:
        trust = bool(cfg["trust_client_launch_folder"]["value"])
    overrides = entry.get("client_workspace_scope_overrides")
    if overrides is None:
        overrides = bool(cfg["client_workspace_scope_overrides"]["value"])
    gateway_mode = str(cfg["workspace_default_mode"]["value"])
    mode = str(entry.get("mode") or "").strip().lower()
    if mode not in _USER_POLICY_MODES:
        mode = gateway_mode
    return {
        "tenant_id": str(tenant_id or "default"),
        "user_id": str(user_id or ""),
        "policy": entry,
        "customized": bool(entry),
        # The gateway rungs an inherit-UI must describe truthfully.
        "gateway_defaults": {
            "mode": gateway_mode,
            "trust_client_launch_folder": bool(cfg["trust_client_launch_folder"]["value"]),
        },
        "effective": {
            "mode": mode,
            "trust_client_launch_folder": bool(trust) or bool(overrides),
            "client_workspace_scope_overrides": bool(overrides),
            "workspace_allowed_paths": list(entry.get("workspace_allowed_paths") or []),
            "workspace_blocked_paths": list(entry.get("workspace_blocked_paths") or []),
        },
    }


def write_user_workspace_policy(
    data_dir: Path,
    *,
    tenant_id: Optional[str],
    user_id: Optional[str],
    policy: Optional[Dict[str, Any]],
    actor: str,
    preserve_fields: tuple[str, ...] = (),
) -> Dict[str, Any]:
    """Write ONE user's policy entry (operator clarification 2026-08-19:
    each user decides their own posture — mode, launch-folder trust, and
    their allow/deny lists). Validates the single entry with the same rules
    as the admin map write; None/{} clears the entry. This function can only
    ever touch the named key.

    `preserve_fields`: fields carried over from the EXISTING entry when the
    incoming policy does not name them — the self-service lane passes the
    admin-classed grant here so a user saving their own card can never
    silently erase what an admin set (design adversary B4)."""
    if not user_id:
        raise RuntimeConfigError("a user identity is required to edit a per-user workspace policy")
    key = _normalize_user_policy_key(f"{tenant_id or 'default'}:{user_id}")

    stored = _read_store(data_dir, strict=True)
    existing = _normalize_user_workspace_policies(stored.get("user_workspace_policies"), strict=False)

    incoming = dict(policy or {})
    if preserve_fields:
        prior = existing.get(key) or {}
        for field in preserve_fields:
            if field not in incoming and field in prior:
                incoming[field] = prior[field]
    validated = _normalize_user_workspace_policies({key: incoming} if incoming else {}, strict=True)

    if key in validated:
        existing[key] = validated[key]
    else:
        existing.pop(key, None)
    if existing:
        stored["user_workspace_policies"] = existing
    else:
        stored.pop("user_workspace_policies", None)

    stored["_last_changed_by"] = str(actor)
    from datetime import datetime, timezone

    stored["_last_changed_at"] = datetime.now(timezone.utc).isoformat()
    _write_store(data_dir, stored)
    return read_user_workspace_policy(data_dir, tenant_id=tenant_id, user_id=user_id)


def resolve_user_workspace_paths(
    data_dir: Path,
    *,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """This principal's EXTRA (allowed, blocked) workspace roots — additive
    on top of the gateway-wide mounts/deny list, never a replacement."""
    entry = _stored_user_policy(data_dir, tenant_id=tenant_id, user_id=user_id)
    allowed: list[Path] = []
    blocked: list[Path] = []
    for field, bucket in (("workspace_allowed_paths", allowed), ("workspace_blocked_paths", blocked)):
        for item in entry.get(field) or []:
            text = str(item or "").strip()
            if not text:
                continue
            try:
                resolved = Path(text).expanduser().resolve()
            except Exception:
                continue
            bucket.append(resolved)
    return tuple(allowed), tuple(blocked)


def resolve_executor(data_dir: Path) -> str:
    """The configured executor, alias-folded to canonical (a stored/env
    legacy spelling like codex_cli still resolves)."""
    raw = str(read_runtime_config(data_dir)["executor"]["value"])
    return canonical_executor_id(raw) or raw


def validate_executor_choice(executor_id: Optional[str]) -> Optional[str]:
    """Per-request executor choice on /backlog/execute (continuum c1550 ask
    2; the target_model precedent c1090). None = use the configured default.
    An unknown id raises; an unavailable one raises naming the roster."""
    if executor_id is None or str(executor_id).strip() == "":
        return None
    exec_id = canonical_executor_id(executor_id)
    if exec_id is None:
        raise RuntimeConfigError(f"unknown executor {str(executor_id).strip()!r} — the registry offers {sorted(_VALID_EXECUTOR_IDS)}")
    entry = next(e for e in _EXECUTOR_REGISTRY if e["id"] == exec_id)
    if not executor_available(entry):
        raise RuntimeConfigError(
            f"executor {exec_id!r} is not available on this host (its binary/package is absent) — "
            f"available: {[e['id'] for e in executor_registry() if e['available']]}"
        )
    return exec_id
