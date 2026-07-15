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
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

# The env names each knob reads today (the FALLBACK rung). Kept as the
# single source so the resolver and any future reader agree.
_ENV_PROCESS_MANAGER = "ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER"
_ENV_TRIAGE_ROOT = "ABSTRACTGATEWAY_TRIAGE_REPO_ROOT"
_ENV_EXEC_RUNNER = "ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER"
_ENV_EXECUTOR = "ABSTRACTGATEWAY_BACKLOG_EXECUTOR"

_DEFAULT_EXECUTOR = "codex"

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
    {"id": "abstractcode", "display": "AbstractCode (framework-native)", "probe": ("py", "abstractcode"),
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


def _store_path(data_dir: Path) -> Path:
    return Path(data_dir) / "config" / "runtime_config.json"


def _read_store(data_dir: Path) -> Dict[str, Any]:
    """The persisted operator choices. A missing or corrupt file reads as
    empty (env/default take over) — never a crash, never a silent partial."""
    path = _store_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}  # corrupt store degrades to env/default; the source field will say "env"/"default"
    return data if isinstance(data, dict) else {}


def _resolve(stored: Dict[str, Any], key: str, env_name: str, default: Any, *, as_bool: bool = False) -> Dict[str, Any]:
    """One knob's {value, source}: stored > env > default. The source names
    the winning rung so a launcher losing an env is VISIBLE, never silent."""
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
    if not is_admin:
        # Redact the path; keep the posture (configured + which rung won).
        triage = {"configured": bool(triage.get("value")), "source": triage["source"]}
    return {
        "writable": bool(is_admin),
        "process_manager": _resolve(stored, "process_manager", _ENV_PROCESS_MANAGER, False, as_bool=True),
        "triage_repo_root": triage,
        "backlog_exec_runner": _resolve(stored, "backlog_exec_runner", _ENV_EXEC_RUNNER, False, as_bool=True),
        "executor": _resolve(stored, "executor", _ENV_EXECUTOR, _DEFAULT_EXECUTOR),
        "executors": executor_registry(),
    }


class RuntimeConfigError(ValueError):
    """A rejected config write — the route maps it to an operator-readable 4xx."""


def write_runtime_config(data_dir: Path, changes: Dict[str, Any], *, actor: str) -> Dict[str, Any]:
    """Persist a PARTIAL update (only the named knobs change; unnamed knobs
    keep their stored value or fall through to env/default). Validates each
    field BEFORE writing — a rejected value never lands. Returns the fresh
    posture (the same shape read_runtime_config serves) plus the actor who
    changed it (principal-stamped in the response, the state-marker precedent)."""
    stored = _read_store(data_dir)
    applied: Dict[str, Any] = {}

    if "process_manager" in changes:
        stored["process_manager"] = _bool(changes["process_manager"], False)
        applied["process_manager"] = stored["process_manager"]
    if "backlog_exec_runner" in changes:
        stored["backlog_exec_runner"] = _bool(changes["backlog_exec_runner"], False)
        applied["backlog_exec_runner"] = stored["backlog_exec_runner"]
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
    if "executor" in changes:
        raw_exec = str(changes["executor"] or "").strip()
        exec_id = canonical_executor_id(raw_exec)
        if exec_id is None:
            raise RuntimeConfigError(
                f"unknown executor {raw_exec!r} — the registry offers {sorted(_VALID_EXECUTOR_IDS)}"
            )
        stored["executor"] = exec_id
        applied["executor"] = exec_id

    if not applied:
        raise RuntimeConfigError(
            "no recognized config keys in the request (one of: process_manager, "
            "backlog_exec_runner, triage_repo_root, executor)"
        )

    stored["_last_changed_by"] = str(actor)
    from datetime import datetime, timezone

    stored["_last_changed_at"] = datetime.now(timezone.utc).isoformat()

    path = _store_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic — a torn config read must never blank the app (the c1526 lesson)

    out = read_runtime_config(data_dir)
    out["applied"] = applied
    out["changed_by"] = str(actor)
    return out


# ---- Resolvers: the CONSUMER-facing read (value only, stored>env>default) ----
# The surface persists values; these are what the actual knob-readers call so
# a stored choice is HONORED, not cosmetic. Each mirrors read_runtime_config's
# precedence for one knob but returns the bare value.

def resolve_process_manager_enabled(data_dir: Path) -> bool:
    return bool(read_runtime_config(data_dir)["process_manager"]["value"])


def resolve_triage_repo_root(data_dir: Path) -> Optional[str]:
    v = read_runtime_config(data_dir)["triage_repo_root"]["value"]
    return str(v) if v else None


def resolve_backlog_exec_runner_enabled(data_dir: Path) -> bool:
    return bool(read_runtime_config(data_dir)["backlog_exec_runner"]["value"])


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
