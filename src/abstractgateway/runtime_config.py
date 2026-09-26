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

# ---- One writer at a time -----------------------------------------------
# Every write is read-modify-replace of the whole store. Two writers at once
# (the console and the CLI, or two console tabs) could each read the old
# file and the second replace would silently drop the first change. The
# lock: a process-wide RLock (threads of this gateway) + an flock on
# `runtime_config.json.lock` (other processes: the CLI, a runner process).
import threading as _threading
from contextlib import contextmanager as _contextmanager
import functools as _functools

_STORE_RLOCK = _threading.RLock()
_STORE_LOCK_DEPTH = _threading.local()


@_contextmanager
def store_lock(data_dir: Path):
    with _STORE_RLOCK:
        depth = getattr(_STORE_LOCK_DEPTH, "n", 0)
        _STORE_LOCK_DEPTH.n = depth + 1
        fh = None
        try:
            if depth == 0:
                lock_path = _store_path(data_dir).with_suffix(".json.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(lock_path, "a+")
                try:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except ImportError:  # pragma: no cover - Windows: the in-process lock still holds
                    pass
            yield
        finally:
            _STORE_LOCK_DEPTH.n = depth
            if fh is not None:
                try:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except ImportError:  # pragma: no cover
                    pass
                fh.close()


def _locked_store_write(fn):
    """Run a store writer (first argument: data_dir) under store_lock."""

    @_functools.wraps(fn)
    def wrapper(data_dir, *args, **kwargs):
        with store_lock(Path(data_dir)):
            return fn(data_dir, *args, **kwargs)

    return wrapper

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


BIND_HOST_ENV = "ABSTRACTGATEWAY_BIND_HOST"


def gateway_bind_host() -> Optional[str]:
    """The host this Gateway process was told to bind (`serve --host`), or None.

    `abstractgateway serve` records it in `ABSTRACTGATEWAY_BIND_HOST` before
    uvicorn starts; a process launched some other way (a bare
    `uvicorn abstractgateway.app:app`, a test client) has no record, and an
    unknown bind counts as NOT loopback wherever that matters.
    """
    raw = str(os.getenv(BIND_HOST_ENV) or "").strip()
    return raw or None


def _bind_is_loopback(host: Optional[str]) -> bool:
    raw = str(host or "").strip().strip("[]").lower()
    if not raw:
        return False
    if raw == "localhost":
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def _allow_engine_install_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    """May an admin install a local engine (Ollama, LM Studio CLI, MLX, …) on
    THIS host from the console / API? An engine install runs a vendor
    installer on the machine that runs the Gateway, which for a remote
    Gateway is not the machine of the person clicking. So the default is ON
    only when the Gateway is bound to loopback (the person at the keyboard IS
    the host) and OFF for any other or unknown bind. A stored choice wins
    either way. No env rung (stored > default, like trust_client_launch_folder).
    Dry runs ("show the command") never need this permission."""
    bind = gateway_bind_host()
    loopback = _bind_is_loopback(bind)
    if "allow_engine_install" in stored and stored["allow_engine_install"] is not None:
        return {"value": _bool(stored["allow_engine_install"], loopback), "source": "stored", "bind_host": bind, "loopback_bind": loopback}
    return {"value": loopback, "source": "default", "bind_host": bind, "loopback_bind": loopback}


def resolve_allow_engine_install(data_dir: Path, *, caller_on_this_machine: bool = False) -> bool:
    return bool(allow_engine_install_for_caller(_allow_engine_install_payload(_read_store(data_dir)), caller_on_this_machine=caller_on_this_machine)["value"])


def allow_engine_install_for_caller(policy: Dict[str, Any], *, caller_on_this_machine: bool) -> Dict[str, Any]:
    """The host policy above, applied to ONE caller (mission HH, 2026-09-24).

    With no stored choice, a caller on the gateway machine itself (loopback
    peer or one of this host's own addresses, no proxy headers:
    `security/same_machine.py`) may install whatever the bind: the person at
    the keyboard IS the host, also when the gateway listens on the LAN. A
    remote caller keeps needing the explicit setting (or a loopback bind). A
    stored choice wins either way (a stored OFF is off for everyone)."""
    out = dict(policy)
    out["caller_on_this_machine"] = bool(caller_on_this_machine)
    if out.get("source") == "default" and not out.get("value") and caller_on_this_machine:
        out["value"] = True
        out["source"] = "default_same_machine"
    return out


# ---- Network exposure (network_exposure.py owns the semantics) -------------
# Stored as {"network": {"exposure": "localhost|lan|internet", "port": int,
# "internet_acknowledged": {"at", "by"} | absent, "allowed_origins": [origin],
# "trust_proxy": bool}}. No env rung for the mode/port (dm#177): the only other
# door is `serve --host/--port`, reported as a CLI override. The two
# reverse-proxy fields keep their historical env names as a DEPLOYMENT
# override (the security carve-out): reported as `overridden_by_env`.


def _network_payload(stored: Dict[str, Any], env: Optional[Any] = None) -> Dict[str, Any]:
    from .network_exposure import DEFAULT_PORT, MODE_BIND_HOST, NetworkSettingError, normalize_mode, validate_port

    raw = stored.get("network") if isinstance(stored.get("network"), dict) else {}
    out: Dict[str, Any] = {}
    mode = None
    if raw.get("exposure") is not None:
        try:
            mode = normalize_mode(raw.get("exposure"))
        except NetworkSettingError:
            out["invalid_exposure"] = raw.get("exposure")
    if mode is not None:
        out.update({"mode": mode, "source": "stored"})
    else:
        from .first_run import default_bind_host

        host = default_bind_host(env)
        out.update({"mode": "localhost" if _bind_is_loopback(host) else "lan", "source": "default", "default_bind_host": host})
    port = None
    if raw.get("port") is not None:
        try:
            port = validate_port(raw.get("port"))
        except NetworkSettingError:
            out["invalid_port"] = raw.get("port")
    out.update({"port": port, "port_source": "stored"} if port is not None else {"port": DEFAULT_PORT, "port_source": "default"})
    ack = raw.get("internet_acknowledged")
    out["internet_acknowledged"] = dict(ack) if isinstance(ack, dict) and out["mode"] == "internet" else None
    out["bind_host"] = out.get("default_bind_host") or MODE_BIND_HOST[out["mode"]]
    # Reverse proxy (mission Z): browser origins allowed on top of the
    # built-in localhost ones, and whether X-Forwarded-For names the client.
    # Stored values only here; network_exposure.reverse_proxy_status adds the
    # env override and the effective values the middleware applies.
    from .network_exposure import normalize_origin

    origins_raw = raw.get("allowed_origins")
    if isinstance(origins_raw, list):
        good: List[str] = []
        bad: List[Dict[str, str]] = []
        for item in origins_raw:
            try:
                o = normalize_origin(item)
            except NetworkSettingError as exc:
                bad.append({"value": str(item), "error": str(exc)})
                continue
            if o not in good:
                good.append(o)
        out["allowed_origins"] = good
        out["allowed_origins_source"] = "stored"
        if bad:
            out["invalid_allowed_origins"] = bad
    else:
        out["allowed_origins"] = []
        out["allowed_origins_source"] = "default"
    if isinstance(raw.get("trust_proxy"), bool):
        out["trust_proxy"] = bool(raw["trust_proxy"])
        out["trust_proxy_source"] = "stored"
    else:
        out["trust_proxy"] = False
        out["trust_proxy_source"] = "default"
    return out


def resolve_network_setting(data_dir: Path, *, env: Optional[Any] = None) -> Dict[str, Any]:
    """{mode, source, port, port_source, internet_acknowledged, bind_host}: the
    stored network exposure, or the historical `serve` default."""
    return _network_payload(_read_store(data_dir), env=env)


@_locked_store_write
def write_network_setting(
    data_dir: Path,
    *,
    mode: Optional[str],
    port: Optional[int],
    internet_acknowledged: Optional[Dict[str, Any]],
    actor: str,
    allowed_origins: Optional[List[str]] = None,
    trust_proxy: Optional[bool] = None,
) -> Dict[str, Any]:
    """Persist a VALIDATED network change (network_exposure.apply_network_change
    is the only caller: it owns the auth and acknowledgement refusals).
    `port=None` keeps the stored port; `mode=None` keeps the stored mode (a
    reverse-proxy-only change). `allowed_origins` / `trust_proxy`: None keeps
    the stored value; a list / a bool replaces it (already validated); `[]`
    clears the list back to the default (built-in origins only)."""
    from datetime import datetime, timezone

    stored = _read_store(data_dir, strict=True)
    net = dict(stored.get("network") or {}) if isinstance(stored.get("network"), dict) else {}
    if mode is not None:
        net["exposure"] = str(mode)
        if internet_acknowledged:
            net["internet_acknowledged"] = dict(internet_acknowledged)
        else:
            net.pop("internet_acknowledged", None)
    if port is not None:
        net["port"] = int(port)
    if allowed_origins is not None:
        if allowed_origins:
            net["allowed_origins"] = [str(o) for o in allowed_origins]
        else:
            net.pop("allowed_origins", None)  # [] = back to the default (built-in origins only)
    if trust_proxy is not None:
        net["trust_proxy"] = bool(trust_proxy)
    stored["network"] = net
    stored["_last_changed_by"] = str(actor)
    stored["_last_changed_at"] = datetime.now(timezone.utc).isoformat()
    _write_store(data_dir, stored)
    return _network_payload(stored)


# ---- Browser apps (apps_manager.py reads these through resolve_apps_setting) --
# Mission Z (operator 2026-09-24: "i explicitly told you i don't like env
# vars"): the five ABSTRACTGATEWAY_APPS_* knobs mission O added become stored
# settings `apps.<name>`, written through the generic runtime-config door
# (POST /api/gateway/admin/runtime-config {"apps.host": ...}) and the CLI
# `abstractgateway apps config get|set`. Precedence is the ruled law of this
# module (dm#194): stored > env > default; the env name is a labeled fallback
# (`source: env`), and an env value a stored one shadows is reported
# (`env_shadowed`). `APPS_SETTINGS` is the registry the console's settings
# card and the CLI render (label + help + default), so a new knob is one row.

APPS_SETTINGS: List[Dict[str, Any]] = [
    {
        "name": "node", "key": "apps.node", "env": "ABSTRACTGATEWAY_APPS_NODE", "default": "auto",
        "label": "Node.js for apps",
        "help": "auto: the Node.js 18+ found on this computer, else the one the gateway installs for you. "
                "managed: always the gateway's own. system: only the one on this computer. "
                "Or the full path of a node program.",
        "placeholder": "auto",
    },
    {
        "name": "ports", "key": "apps.ports", "env": "ABSTRACTGATEWAY_APPS_PORTS", "default": "",
        "label": "Ports for apps",
        "help": "A port or a range, e.g. 3100-3199. Empty: each app's usual port, else the next free one in 3100-3199.",
        "placeholder": "3100-3199",
    },
    {
        "name": "host", "key": "apps.host", "env": "ABSTRACTGATEWAY_APPS_HOST", "default": "127.0.0.1",
        "label": "Where apps listen",
        "help": "127.0.0.1 = this computer only. 0.0.0.0 = every network this computer is on (other machines can "
                "open the apps; put them behind your own access control). Applies when an app next starts.",
        "placeholder": "127.0.0.1",
    },
    {
        "name": "npm_registry", "key": "apps.npm_registry", "env": "ABSTRACTGATEWAY_APPS_NPM_REGISTRY",
        "default": "https://registry.npmjs.org",
        "label": "npm registry",
        "help": "Where apps are downloaded from (a company mirror, for example).",
        "placeholder": "https://registry.npmjs.org",
    },
    {
        "name": "pypi_url", "key": "apps.pypi_url", "env": "ABSTRACTGATEWAY_APPS_PYPI_URL",
        "default": "https://pypi.org/pypi",
        "label": "Node.js download index",
        "help": "The Python package index the gateway looks up its own Node.js build on (a mirror, for example).",
        "placeholder": "https://pypi.org/pypi",
    },
]
_APPS_BY_NAME: Dict[str, Dict[str, Any]] = {row["name"]: row for row in APPS_SETTINGS}
_APPS_NODE_MODES = ("auto", "managed", "system")


def _validate_apps_value(name: str, raw: Any) -> str:
    """One apps setting, validated; raises RuntimeConfigError in words."""
    text = str(raw if raw is not None else "").strip()
    key = f"apps.{name}"
    if name == "node":
        if text in _APPS_NODE_MODES:
            return text
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise RuntimeConfigError(f"{key} is auto, managed, system, or the full path of a node program (got {text!r})")
        if not path.is_file():
            raise RuntimeConfigError(f"{key}: no file at {text}")
        return str(path)
    if name == "ports":
        m = re.match(r"^(\d{1,5})\s*-\s*(\d{1,5})$", text)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
        elif text.isdigit():
            lo = hi = int(text)
        else:
            raise RuntimeConfigError(f"{key} is a port or a range like 3100-3199 (got {text!r})")
        if not (1 <= lo <= hi <= 65535):
            raise RuntimeConfigError(f"{key}: {text!r} is not a valid port range (1-65535, low-high)")
        return f"{lo}-{hi}" if lo != hi else str(lo)
    if name == "host":
        h = text.strip("[]")
        try:
            import ipaddress

            return str(ipaddress.ip_address(h))
        except ValueError:
            pass
        if h.lower() == "localhost" or re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$", h):
            return h.lower()
        raise RuntimeConfigError(f"{key} is an IP address or a host name, e.g. 127.0.0.1 or 0.0.0.0 (got {text!r})")
    if name in ("npm_registry", "pypi_url"):
        from urllib.parse import urlsplit

        u = urlsplit(text)
        if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment:
            raise RuntimeConfigError(f"{key} is an http(s) URL, e.g. {_APPS_BY_NAME[name]['default']} (got {text!r})")
        return text.rstrip("/")
    raise RuntimeConfigError(f"unknown apps setting {key!r}; known: {sorted(r['key'] for r in APPS_SETTINGS)}")


def _apps_setting_payload(stored: Dict[str, Any], name: str, env: Optional[Any] = None) -> Dict[str, Any]:
    row = _APPS_BY_NAME[name]  # KeyError on an unknown name: callers name a registered knob
    env = os.environ if env is None else env
    apps_stored = stored.get("apps") if isinstance(stored.get("apps"), dict) else {}
    env_raw = env.get(row["env"])
    env_set = env_raw is not None and str(env_raw).strip() != ""
    out: Dict[str, Any] = {
        "key": row["key"], "label": row["label"], "help": row["help"], "placeholder": row["placeholder"],
        "default": row["default"], "env_name": row["env"],
    }
    if name in apps_stored and apps_stored[name] is not None:
        try:
            out.update({"value": _validate_apps_value(name, apps_stored[name]), "source": "stored"})
            if env_set:
                out["env_shadowed"] = True
                out["note"] = f"the stored value wins over {row['env']} in this gateway's environment"
            return out
        except RuntimeConfigError as exc:
            out["invalid_stored"] = f"{apps_stored[name]!r}: {exc}"
    if env_set:
        try:
            out.update({"value": _validate_apps_value(name, env_raw), "source": "env"})
            out["note"] = f"from {row['env']} in the environment this gateway was started with; a saved value replaces it"
            return out
        except RuntimeConfigError as exc:
            out["invalid_env"] = f"{row['env']}={env_raw!r}: {exc}"
    out.update({"value": row["default"], "source": "default"})
    return out


def _apps_settings_payload(stored: Dict[str, Any]) -> Dict[str, Any]:
    return {row["name"]: _apps_setting_payload(stored, row["name"]) for row in APPS_SETTINGS}


def resolve_apps_setting(data_dir: Path, name: str) -> Dict[str, Any]:
    """{value, source, ...} for one browser-apps setting (stored > env >
    default). THE read apps_manager uses; an unknown `name` raises KeyError
    (a typo must fail loudly, never read as the default). An invalid stored or
    env value falls to the next rung and says so (`invalid_stored` /
    `invalid_env`). Read at each use, so a change applies to the next action
    (an app's next start for `host`/`ports`, the next download for the URLs)."""
    if name not in _APPS_BY_NAME:
        raise KeyError(f"unknown apps setting {name!r}; known: {sorted(_APPS_BY_NAME)}")
    return _apps_setting_payload(_read_store(data_dir), name)


def _write_apps_changes(stored: Dict[str, Any], changes: Dict[str, Any], applied: Dict[str, Any]) -> None:
    """`apps.<name>` keys (flat) or {"apps": {name: value}} (nested) -> stored["apps"]."""
    pairs: List[tuple] = []
    for k, v in changes.items():
        if isinstance(k, str) and k.startswith("apps."):
            pairs.append((k[len("apps."):], v))
    if isinstance(changes.get("apps"), dict):
        pairs.extend(changes["apps"].items())
    elif "apps" in changes:
        raise RuntimeConfigError("apps must be an object {name: value} (or use flat keys like \"apps.host\")")
    if not pairs:
        return
    apps_stored = dict(stored.get("apps") or {}) if isinstance(stored.get("apps"), dict) else {}
    for name, raw in pairs:
        if name not in _APPS_BY_NAME:
            raise RuntimeConfigError(f"unknown apps setting 'apps.{name}'; known: {sorted(r['key'] for r in APPS_SETTINGS)}")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            apps_stored.pop(name, None)  # clear = fall back to env/default
            applied[f"apps.{name}"] = None
        else:
            apps_stored[name] = _validate_apps_value(name, raw)
            applied[f"apps.{name}"] = apps_stored[name]
    if apps_stored:
        stored["apps"] = apps_stored
    else:
        stored.pop("apps", None)


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


# ---- Backlog folder + exec runner (mission II, operator 2026-09-24: "fix
# continuum for a new fresh install ... handled with proper settings and
# --param_name") ----------------------------------------------------------
#
# ONE resolution, used by every consumer (the backlog routes, the process
# manager, the exec runner, triage, the settings surfaces):
#
#   backlog folder (key `triage_repo_root`):
#       `serve --backlog-root PATH`  (source "flag")
#     > the stored setting            (source "stored")
#     > the legacy environment        (source "env" — reported, never taught)
#     > <data dir>/backlog            (source "default"; the standard skeleton
#                                      is created there on first use)
#
#   exec runner (key `backlog_exec_runner`):
#       `serve --exec-runner on|off` > stored > legacy env > off
#
# The launch flags reach the resolver through a small record the serving
# process writes under <data dir>/run/ (and removes when it stops): the value
# must reach `serve --reload` workers and the CLI's `config get` without an
# environment variable. A record whose process is gone is ignored.

_ENV_TRIAGE_ROOT_LEGACY = ("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", "ABSTRACT_TRIAGE_REPO_ROOT")
_ENV_EXEC_RUNNER_LEGACY = ("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", "ABSTRACT_BACKLOG_EXEC_RUNNER")
BACKLOG_DEFAULT_DIRNAME = "backlog"
BACKLOG_SUBPATH = ("docs", "backlog")
BACKLOG_FOLDERS = ("planned", "proposed", "completed")
_LAUNCH_SETTINGS_RELPATH = ("run", "launch-settings.json")
_LAUNCH_KEYS = ("triage_repo_root", "backlog_exec_runner")

# Labels and help for the settings surfaces (console, Continuum, CLI) — one
# row per knob so every door says the same thing.
BACKLOG_SETTINGS: List[Dict[str, Any]] = [
    {
        "key": "triage_repo_root",
        "label": "Backlog folder",
        "help": "The folder whose docs/backlog holds the backlog items Continuum shows. Empty: the gateway's own "
                "folder (<data dir>/backlog), created with a starter overview and template on first use. "
                "Another folder must already contain docs/backlog.",
        "flag": "--backlog-root",
        "cli": "abstractgateway config set triage_repo_root PATH",
    },
    {
        "key": "backlog_exec_runner",
        "label": "Backlog exec runner",
        "help": "Runs the backlog items queued for execution (Continuum's Execute) on this machine with the chosen "
                "agent. Off by default: only turn it on for a gateway you trust with running code.",
        "flag": "--exec-runner on|off",
        "cli": "abstractgateway config set backlog_exec_runner on|off",
    },
    {
        "key": "process_manager",
        "label": "Process manager",
        "help": "Powers Continuum's Services page (start, stop and redeploy the framework's processes). "
                "Whoever reaches Services can redeploy: keep it off unless you need it.",
        "flag": None,
        "cli": "abstractgateway config set process_manager on|off",
    },
]
_BACKLOG_SETTINGS_BY_KEY: Dict[str, Dict[str, Any]] = {row["key"]: row for row in BACKLOG_SETTINGS}


def _strict_bool(key: str, raw: Any) -> bool:
    """on/off for a switch written through a door; garbage refuses in words
    (the lenient `_bool` would store `False` for a typo like "of")."""
    if isinstance(raw, bool):
        return raw
    s = str(raw if raw is not None else "").strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    raise RuntimeConfigError(f"{key} is on or off (got {raw!r})")


def default_backlog_root(data_dir: Path) -> Path:
    """The gateway's own backlog folder: <data dir>/backlog."""
    return Path(data_dir).expanduser().resolve() / BACKLOG_DEFAULT_DIRNAME


def _skeleton_source(name: str) -> str:
    from importlib import resources

    return (resources.files("abstractgateway") / "assets" / "backlog_skeleton" / name).read_text(encoding="utf-8")


def ensure_backlog_skeleton(root: Path) -> List[str]:
    """Create <root>/docs/backlog with overview.md, template.md and the three
    state folders. Never overwrites: only what is missing is created. Returns
    the created paths (relative to root)."""
    created: List[str] = []
    backlog = Path(root).joinpath(*BACKLOG_SUBPATH)
    if not backlog.is_dir():
        backlog.mkdir(parents=True, exist_ok=True)
        created.append("/".join(BACKLOG_SUBPATH))
    for folder in BACKLOG_FOLDERS:
        p = backlog / folder
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            created.append("/".join((*BACKLOG_SUBPATH, folder)))
    for name in ("overview.md", "template.md"):
        p = backlog / name
        if not p.exists():
            p.write_text(_skeleton_source(name), encoding="utf-8")
            created.append("/".join((*BACKLOG_SUBPATH, name)))
    return created


def _launch_settings_path(data_dir: Path) -> Path:
    return Path(data_dir).joinpath(*_LAUNCH_SETTINGS_RELPATH)


def _pid_alive(pid: Any) -> bool:
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return False
    if n <= 0:
        return False
    try:
        os.kill(n, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def record_launch_settings(data_dir: Path, values: Dict[str, Any], *, pid: Optional[int] = None) -> None:
    """Called by `serve` with the launch flags it was given (only the keys it
    was given). No flags = the record is removed, so a previous launch's flag
    never outlives it."""
    path = _launch_settings_path(data_dir)
    clean = {k: v for k, v in (values or {}).items() if k in _LAUNCH_KEYS and v is not None}
    if not clean:
        clear_launch_settings(data_dir)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps({"pid": int(pid or os.getpid()), "values": clean}, indent=2), encoding="utf-8")
    tmp.replace(path)


def clear_launch_settings(data_dir: Path, *, pid: Optional[int] = None) -> None:
    """Remove the launch-flag record; with `pid`, only when that process
    wrote it (the app's shutdown hook: SIGTERM never reaches the CLI's
    `finally`, and a stale record is ignored anyway by its pid check)."""
    path = _launch_settings_path(data_dir)
    if pid is not None:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(rec, dict) or int(rec.get("pid") or -1) != int(pid):
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_launch_settings(data_dir: Path) -> Dict[str, Any]:
    """The live serving process's launch flags ({} when none, or when the
    process that wrote them is gone)."""
    path = _launch_settings_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        import logging

        logging.getLogger("abstractgateway.runtime_config").warning(
            "#FALLBACK launch-settings record unreadable (%s) — launch flags ignored", path
        )
        return {}
    if not isinstance(data, dict) or not _pid_alive(data.get("pid")):
        return {}
    values = data.get("values")
    return {k: v for k, v in values.items() if k in _LAUNCH_KEYS} if isinstance(values, dict) else {}


def _legacy_env(names: tuple) -> Optional[tuple]:
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip():
            return name, str(raw).strip()
    return None


def backlog_root_problem(path: Path, data_dir: Path, *, require_backlog: bool = True) -> Optional[str]:
    """Why `path` cannot serve as the backlog folder (None = it can). The
    gateway's own folder always can: its skeleton is created on use.

    `require_backlog` (the WRITE doors: settings, `--backlog-root`) also
    demands docs/backlog. The READ side only needs the folder to exist: a
    folder whose docs/backlog is missing lists no items, and the process
    manager uses the same folder as its repo root (which need not hold a
    backlog)."""
    p = Path(path).expanduser()
    try:
        if p.resolve() == default_backlog_root(data_dir):
            return None
    except Exception:
        pass
    if not p.exists():
        return "the folder does not exist"
    if not p.is_dir():
        return "it is a file, not a folder"
    if require_backlog and not p.joinpath(*BACKLOG_SUBPATH).is_dir():
        return "it has no docs/backlog folder"
    return None


def validate_backlog_root(raw: Any, data_dir: Path, *, what: str = "triage_repo_root") -> Path:
    """The validation every door uses (settings write, `serve --backlog-root`,
    `config set`). Returns the resolved path; refuses in a plain sentence
    naming what is missing and the one-step alternative."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise RuntimeConfigError(f"{what} needs a folder path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    problem = backlog_root_problem(path, data_dir)
    if problem:
        raise RuntimeConfigError(
            f"{what} {text!r} cannot be the backlog folder: {problem}. "
            f"Choose a folder that contains docs/backlog, or use the gateway's own folder "
            f"{default_backlog_root(data_dir)} (created for you)."
        )
    resolved = path.resolve()
    if resolved == default_backlog_root(data_dir):
        ensure_backlog_skeleton(resolved)
    return resolved


def resolve_backlog_root(
    data_dir: Path,
    *,
    stored: Optional[Dict[str, Any]] = None,
    launch: Optional[Dict[str, Any]] = None,
    ensure: bool = True,
) -> Dict[str, Any]:
    """THE backlog-folder resolution (flag > stored > legacy env > default).

    Returns {value, source, available, reason, default_path, backlog_dir,
    env_shadowed?, env_name?, created?}. `available` False carries a plain
    `reason` (the path is in `value`, never in `reason`, so the reason can be
    shown to non-admins). `ensure` creates the default folder's skeleton —
    the "first use" — and is what every consumer passes; the settings read
    passes False (a GET never writes)."""
    data_dir = Path(data_dir)
    if stored is None:
        stored = _read_store(data_dir)
    if launch is None:
        launch = read_launch_settings(data_dir)
    default_path = default_backlog_root(data_dir)
    env_hit = _legacy_env(_ENV_TRIAGE_ROOT_LEGACY)
    raw: Optional[str] = None
    source = "default"
    if launch.get("triage_repo_root"):
        raw, source = str(launch["triage_repo_root"]), "flag"
    elif stored.get("triage_repo_root"):
        raw, source = str(stored["triage_repo_root"]), "stored"
    elif env_hit is not None:
        raw, source = env_hit[1], "env"
    out: Dict[str, Any] = {"source": source, "default_path": str(default_path)}
    if source == "flag" and stored.get("triage_repo_root"):
        out["stored_value"] = str(stored["triage_repo_root"])  # saved, applies once the flag is gone
    if env_hit is not None:
        out["env_name"] = env_hit[0]
        if source in ("flag", "stored"):
            out["env_shadowed"] = True
    if raw is None:
        path = default_path
    else:
        try:
            path = Path(raw).expanduser().resolve()
        except Exception:
            path = Path(raw).expanduser()
    out["value"] = str(path)
    out["backlog_dir"] = str(path.joinpath(*BACKLOG_SUBPATH))
    if path == default_path:
        if ensure:
            try:
                created = ensure_backlog_skeleton(path)
            except OSError as exc:
                out.update({"available": False, "reason": f"the gateway could not create its backlog folder ({exc.strerror or exc})"})
                return out
            if created:
                out["created"] = created
            out.update({"available": True, "reason": None})
        else:
            exists = path.joinpath(*BACKLOG_SUBPATH).is_dir()
            out.update({"available": True, "reason": None, "exists": exists})
        return out
    problem = backlog_root_problem(path, data_dir, require_backlog=False)
    rung = {"flag": "the --backlog-root launch flag", "stored": "the saved setting", "env": "the environment"}[source]
    if problem:
        out.update({"available": False, "reason": f"{problem} (set by {rung})"})
    else:
        out.update({"available": True, "reason": None})
    return out


def resolve_exec_runner(data_dir: Path, *, stored: Optional[Dict[str, Any]] = None, launch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{value, source} for the backlog exec runner (flag > stored > legacy env > off)."""
    if stored is None:
        stored = _read_store(Path(data_dir))
    if launch is None:
        launch = read_launch_settings(Path(data_dir))
    env_hit = _legacy_env(_ENV_EXEC_RUNNER_LEGACY)
    out: Dict[str, Any]
    if "backlog_exec_runner" in launch and launch["backlog_exec_runner"] is not None:
        out = {"value": _bool(launch["backlog_exec_runner"], False), "source": "flag"}
    elif "backlog_exec_runner" in stored and stored["backlog_exec_runner"] is not None:
        out = {"value": _bool(stored["backlog_exec_runner"], False), "source": "stored"}
    elif env_hit is not None:
        out = {"value": _bool(env_hit[1], False), "source": "env"}
    else:
        out = {"value": False, "source": "default"}
    if out["source"] == "flag" and stored.get("backlog_exec_runner") is not None:
        out["stored_value"] = _bool(stored["backlog_exec_runner"], False)  # saved, applies once the flag is gone
    if env_hit is not None:
        out["env_name"] = env_hit[0]
        if out["source"] in ("flag", "stored"):
            out["env_shadowed"] = True
    return out


def _with_setting_meta(entry: Dict[str, Any], key: str) -> Dict[str, Any]:
    row = _BACKLOG_SETTINGS_BY_KEY[key]
    out = dict(entry)
    out.update({"key": key, "label": row["label"], "help": row["help"], "cli": row["cli"]})
    if row.get("flag"):
        out["flag"] = row["flag"]
    return out


def _redact_backlog_root(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Non-admin view: posture without server paths."""
    keep = {k: entry[k] for k in ("source", "available", "reason", "key", "label", "help", "cli", "flag") if k in entry}
    keep["configured"] = bool(entry.get("value"))
    return keep


def _agents_payload(data_dir: Path, agent_index: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The `agents` block (default agent workflow per interface). Resolved
    against the live host's entrypoints when the caller passes them, else
    against the flows folder on disk (`index_source` says which)."""
    from .agent_defaults import default_workflows_payload, offline_entrypoint_index

    source = "host"
    if agent_index is None:
        source = "flows_dir"
        try:
            agent_index = offline_entrypoint_index()
        except Exception as exc:  # noqa: BLE001 - say it; never an empty-looking success
            return {"default_workflow": {}, "index_source": source, "error": f"the workflows could not be read: {exc}"}
    out = default_workflows_payload(agent_index, Path(data_dir))
    out["index_source"] = source
    return out


def _write_agents_changes(
    stored: Dict[str, Any],
    changes: Dict[str, Any],
    applied: Dict[str, Any],
    agent_index: Optional[List[Dict[str, Any]]],
) -> None:
    """`agents.default_workflow.<interface>` (flat) or
    {"agents": {"default_workflow": {interface: value}}} (nested). "" or
    null clears (back to the built-in default). Each value must resolve on
    this gateway and declare the interface (agent_defaults validates)."""
    from .agent_defaults import SETTING_KEY, DefaultWorkflowError, offline_entrypoint_index, validate_default_workflow_value

    pairs: List[tuple] = []
    prefix = SETTING_KEY + "."
    for k, v in changes.items():
        if isinstance(k, str) and k.startswith(prefix):
            pairs.append((k[len(prefix):], v))
        elif isinstance(k, str) and k.startswith("agents.") and k != "agents":
            raise RuntimeConfigError(f"unknown setting {k!r}; the agents settings are {SETTING_KEY}.<interface>")
    if "agents" in changes:
        block = changes["agents"]
        if not isinstance(block, dict) or set(block) - {"default_workflow"} or not isinstance(block.get("default_workflow", {}), dict):
            raise RuntimeConfigError('agents must be {"default_workflow": {"<interface>": "bundle[@version]:flow" | ""}}')
        pairs.extend((block.get("default_workflow") or {}).items())
    if not pairs:
        return
    agents = dict(stored.get("agents") or {}) if isinstance(stored.get("agents"), dict) else {}
    mapping = dict(agents.get("default_workflow") or {}) if isinstance(agents.get("default_workflow"), dict) else {}
    for iface, raw in pairs:
        iface = str(iface or "").strip()
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            mapping.pop(iface, None)
            applied[f"{SETTING_KEY}.{iface}"] = None
            continue
        if agent_index is None:
            agent_index = offline_entrypoint_index()
        try:
            mapping[iface] = validate_default_workflow_value(iface, raw, index=agent_index)
        except DefaultWorkflowError as exc:
            raise RuntimeConfigError(str(exc)) from exc
        applied[f"{SETTING_KEY}.{iface}"] = mapping[iface]
    if mapping:
        agents["default_workflow"] = mapping
    else:
        agents.pop("default_workflow", None)
    if agents:
        stored["agents"] = agents
    else:
        stored.pop("agents", None)


def read_runtime_config(
    data_dir: Path,
    *,
    is_admin: bool = True,
    agent_index: Optional[List[Dict[str, Any]]] = None,
    include_agents: bool = False,
    include_skills: bool = False,
) -> Dict[str, Any]:
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
    launch = read_launch_settings(data_dir)
    triage = _with_setting_meta(
        resolve_backlog_root(data_dir, stored=stored, launch=launch, ensure=False), "triage_repo_root"
    )
    workspace_root = _workspace_root_payload(stored)
    workspace_mounts = _workspace_mounts_payload(stored)
    workspace_allowed_paths = _workspace_allowed_paths_payload(stored)
    workspace_blocked_paths = _workspace_blocked_paths_payload(stored)
    user_workspace_policies = _user_workspace_policies_payload(stored)
    if not is_admin:
        # Redact the path; keep the posture (configured + which rung won).
        triage = _redact_backlog_root(triage)
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
    out: Dict[str, Any] = {
        "writable": bool(is_admin),
        "process_manager": _with_setting_meta(
            _resolve(stored, "process_manager", _ENV_PROCESS_MANAGER, False, as_bool=True), "process_manager"
        ),
        "triage_repo_root": triage,
        "workspace_root": workspace_root,
        "workspace_mounts": workspace_mounts,
        "workspace_allowed_paths": workspace_allowed_paths,
        "workspace_blocked_paths": workspace_blocked_paths,
        "client_workspace_scope_overrides": _client_workspace_scope_overrides_payload(stored),
        "trust_client_launch_folder": _trust_client_launch_folder_payload(stored),
        "workspace_default_mode": _workspace_default_mode_payload(stored),
        "user_workspace_policies": user_workspace_policies,
        "builtin_deny": _workspace_builtin_deny_payload(stored, data_dir)
        if is_admin
        else {k: v for k, v in _workspace_builtin_deny_payload(stored, data_dir).items() if k != "value"},
        "backlog_exec_runner": _with_setting_meta(
            resolve_exec_runner(data_dir, stored=stored, launch=launch), "backlog_exec_runner"
        ),
        "executor": _resolve(stored, "executor", _ENV_EXECUTOR, _DEFAULT_EXECUTOR),
        "executors": executor_registry(),
        "operator_email": operator_email,
        "stop_kill_switch_s": _stop_kill_switch_seconds_payload(stored),
        "allow_engine_install": _allow_engine_install_payload(stored),
        # Read-only here: GET/POST /api/gateway/network is the door (auth and
        # acknowledgement refusals, addresses, restart story).
        "network": _network_payload(stored),
        # Browser apps: {name: {value, source, key, label, help, default,
        # env_name, ...}} (registry APPS_SETTINGS); written as "apps.<name>".
        "apps": _apps_settings_payload(stored),
    }
    if agent_index is not None or include_agents:
        # Default agent workflow per agent interface (agent_defaults.py):
        # {default_workflow: {interface: {value, source, available, reason,
        # resolved, key, default, eligible[]}}, index_source}. Only on
        # request: the per-knob resolvers below read this function too, and
        # must not scan workflows to answer "what is the workspace root".
        out["agents"] = _agents_payload(data_dir, agent_index)
    if include_skills:
        # skills.shelf (skills_shelf.py): {key, value, source: stored|env|
        # seeded|checkout|none, resolved, available, reason, default_path,
        # bundled_version, ...}. Non-admins see the posture, not the paths.
        out["skills"] = {"shelf": _skills_shelf_payload(data_dir, is_admin=is_admin)}
    return out


def _skills_shelf_payload(data_dir: Path, *, is_admin: bool) -> Dict[str, Any]:
    from .skills_shelf import shelf_setting_payload

    checkout = None
    try:
        # ensure=False: reading a setting never creates the backlog folder.
        res = resolve_backlog_root(Path(data_dir), ensure=False)
        checkout = Path(str(res["value"])).expanduser().resolve() if res.get("available") and res.get("value") else None
    except Exception:  # noqa: BLE001 - the checkout rung is optional
        checkout = None
    row = shelf_setting_payload(Path(data_dir), checkout_root=checkout)
    if not is_admin:
        keep = {k: row[k] for k in ("key", "label", "help", "source", "available", "bundled_version", "cli") if k in row}
        keep["configured"] = bool(row.get("value"))
        return keep
    return row


def _write_skills_changes(stored: Dict[str, Any], changes: Dict[str, Any], applied: Dict[str, Any]) -> None:
    """`skills.shelf` (flat) or {"skills": {"shelf": PATH}}; "" clears (back
    to the environment, else the gateway's seeded copy)."""
    from .skills_shelf import SETTING_KEY, validate_shelf_value

    pairs: List[tuple] = []
    for k, v in changes.items():
        if k == SETTING_KEY:
            pairs.append(("shelf", v))
        elif isinstance(k, str) and k.startswith("skills."):
            raise RuntimeConfigError(f"unknown setting {k!r}; the skills setting is {SETTING_KEY}")
    if "skills" in changes:
        block = changes["skills"]
        if not isinstance(block, dict) or set(block) - {"shelf"}:
            raise RuntimeConfigError('skills must be {"shelf": "<folder>" | ""}')
        pairs.extend(block.items())
    if not pairs:
        return
    skills = dict(stored.get("skills") or {}) if isinstance(stored.get("skills"), dict) else {}
    for name, raw in pairs:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            skills.pop(name, None)
            applied[SETTING_KEY] = None
        else:
            skills[name] = validate_shelf_value(raw)
            applied[SETTING_KEY] = skills[name]
    if skills:
        stored["skills"] = skills
    else:
        stored.pop("skills", None)


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


# Keys write_runtime_config accepts (plus the prefixed families checked in
# _is_known_write_key). `desktop_tray` is known only to be refused in words.
_WRITE_KEYS = frozenset({
    "process_manager", "backlog_exec_runner", "desktop_tray", "triage_repo_root", "workspace_root",
    "workspace_mounts", "workspace_allowed_paths", "workspace_blocked_paths",
    "client_workspace_scope_overrides", "trust_client_launch_folder", "workspace_default_mode",
    "user_workspace_policies", "executor", "operator_email", "stop_kill_switch_s",
    "allow_engine_install", "apps", "agents", "skills", "workspace_builtin_deny",
})


def _is_known_write_key(key: Any) -> bool:
    k = str(key)
    return k in _WRITE_KEYS or k.startswith("apps.") or k.startswith("agents.") or k.startswith("skills.")


@_locked_store_write
def write_runtime_config(
    data_dir: Path,
    changes: Dict[str, Any],
    *,
    actor: str,
    agent_index: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
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

    if any(k in changes for k in ("network", "network_exposure", "network_port", "allowed_origins", "trust_proxy")):
        # One door for exposure changes: it refuses a network mode without
        # user auth and `internet` without an acknowledgement, and validates
        # origins; this generic write would store any of them silently.
        raise RuntimeConfigError(
            "network exposure and the reverse-proxy settings are changed through POST /api/gateway/network "
            "{mode?, port?, acknowledge_internet?, allowed_origins?, trust_proxy?} (or `abstractgateway network set`), "
            "which checks the auth the mode requires and validates every origin"
        )
    unknown = sorted(str(k) for k in changes if not _is_known_write_key(k))
    if unknown:
        # All or nothing: a request naming a key this gateway does not know
        # (a typo, a newer client) changes NOTHING, rather than saving the
        # keys it knows and quietly dropping the rest.
        raise RuntimeConfigError(
            f"unknown setting(s) {unknown}; nothing was saved. Known: {sorted(_WRITE_KEYS)} "
            "plus apps.<name>, agents.default_workflow.<interface>, skills.shelf"
        )
    for _switch in ("process_manager", "backlog_exec_runner"):
        if _switch in changes:
            raw_switch = changes[_switch]
            if raw_switch is None or (isinstance(raw_switch, str) and not raw_switch.strip()):
                stored.pop(_switch, None)  # clear = fall back to the launch flag / env / default
                applied[_switch] = None
            else:
                stored[_switch] = _strict_bool(_switch, raw_switch)
                applied[_switch] = stored[_switch]
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
            stored.pop("triage_repo_root", None)  # clear = fall back to the launch flag / env / default
            applied["triage_repo_root"] = None
        else:
            # One validation for every door (mission II): an existing folder
            # holding docs/backlog, or the gateway's own folder (created).
            stored["triage_repo_root"] = str(validate_backlog_root(raw, data_dir))
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

    if "workspace_builtin_deny" in changes:
        raw_bd = changes["workspace_builtin_deny"]
        if raw_bd is None or (isinstance(raw_bd, str) and not raw_bd.strip()):
            stored.pop("workspace_builtin_deny", None)  # clear = on
            applied["workspace_builtin_deny"] = None
        else:
            stored["workspace_builtin_deny"] = _strict_bool("workspace_builtin_deny", raw_bd)
            applied["workspace_builtin_deny"] = stored["workspace_builtin_deny"]

    if "allow_engine_install" in changes:
        raw_allow = changes["allow_engine_install"]
        if raw_allow is None or (isinstance(raw_allow, str) and not raw_allow.strip()):
            stored.pop("allow_engine_install", None)  # clear = default (on for a loopback bind or a caller on this machine)
            applied["allow_engine_install"] = None
        else:
            stored["allow_engine_install"] = _bool(raw_allow, False)
            applied["allow_engine_install"] = stored["allow_engine_install"]

    _write_apps_changes(stored, changes, applied)
    _write_agents_changes(stored, changes, applied, agent_index)
    _write_skills_changes(stored, changes, applied)

    if not applied:
        raise RuntimeConfigError(
            "no recognized config keys in the request (one of: process_manager, "
            "backlog_exec_runner, triage_repo_root, workspace_root, workspace_mounts, "
            "workspace_allowed_paths, workspace_blocked_paths, "
            "client_workspace_scope_overrides, trust_client_launch_folder, "
            "workspace_default_mode, user_workspace_policies, executor, operator_email, "
            "stop_kill_switch_s, allow_engine_install, "
            + ", ".join(r["key"] for r in APPS_SETTINGS)
            + ", agents.default_workflow.<interface>, skills.shelf)"
        )

    stored["_last_changed_by"] = str(actor)
    from datetime import datetime, timezone

    stored["_last_changed_at"] = datetime.now(timezone.utc).isoformat()
    _write_store(data_dir, stored)

    touched_agents = any(str(k).startswith("agents.default_workflow.") for k in applied)
    out = read_runtime_config(
        data_dir, agent_index=agent_index, include_agents=touched_agents, include_skills="skills.shelf" in applied
    )
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
    """The backlog folder every consumer uses, or None when it is not
    available (a vanished stored path, for example — resolve_backlog_root
    says why). Creates the gateway's own folder on first use."""
    res = resolve_backlog_root(Path(data_dir), ensure=True)
    return str(res["value"]) if res.get("available") else None


def resolve_backlog_exec_runner_enabled(data_dir: Path) -> bool:
    return bool(resolve_exec_runner(Path(data_dir))["value"])


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


def _workspace_builtin_deny_payload(stored: Dict[str, Any], data_dir: Path) -> Dict[str, Any]:
    """`builtin_deny`: the credential/config folders and the gateway data
    folder that the workspace routes never serve and that runs' tools are
    denied by default (`enabled`: the runs part; an admin may turn it off)."""
    from .workspace_browse import builtin_deny_paths

    raw = stored.get("workspace_builtin_deny")
    enabled = raw if isinstance(raw, bool) else True
    return {
        "value": [str(p) for p in builtin_deny_paths(Path(data_dir))],
        "enabled": bool(enabled),
        "source": "stored" if isinstance(raw, bool) else "default",
        "key": "workspace_builtin_deny",
        "help": "Always hidden from the workspace browser. For runs, a default an admin may turn off.",
    }


def resolve_workspace_builtin_deny_enabled(data_dir: Path) -> bool:
    raw = _read_store(Path(data_dir)).get("workspace_builtin_deny")
    return raw if isinstance(raw, bool) else True


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


@_locked_store_write
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
