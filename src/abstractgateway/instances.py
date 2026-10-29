"""Gateway instances: several self-contained gateways for one user on one machine.

ONE NAME DRIVES EVERYTHING. `abstractgateway --instance NAME <command>` (or
`ABSTRACTGATEWAY_INSTANCE=NAME`, which the service unit carries) selects an instance; nothing is
hand-picked per command:

- data dir: the per-OS default with `-NAME` appended (`~/.local/share/abstractgateway-NAME`,
  `~/Library/Application Support/AbstractGateway-NAME`); an explicit `--data-dir` still wins;
- AbstractCore config: `<data dir>/core-config` (`ABSTRACTCORE_CONFIG_DIR`, unless set), so an
  instance's model routes and provider connections are its own;
- app ports: a block derived from the instance's SLOT, so two instances never collide: slot 0 (the
  default instance) keeps the stack ports 3001-3006 and the fallback range 3100-3199; slot N (N >= 1)
  uses 3001+200N.. and 3100+200N..3199+200N;
- service: `abstractgateway-NAME.service` (systemd user unit), `ai.abstractframework.gateway.NAME`
  (LaunchAgent), `abstractgateway-NAME.desktop` (XDG), `AbstractGateway-NAME` (Windows Run value);
- the console and `GET /api/gateway/about` show the instance name.

THE SLOT. `<config home>/abstractgateway/instances.json` (`~/.config/...` on Linux and macOS,
`%APPDATA%` on Windows) records each named instance's slot, assigned once (the smallest free one) and
never reused while the instance is registered: deterministic per instance, collision-free across
instances. The default instance is slot 0 and is never written there.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple

INSTANCE_ENV = "ABSTRACTGATEWAY_INSTANCE"
DEFAULT_INSTANCE = "default"
REGISTRY_SCHEMA = "gateway_instances_v1"
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$|^[a-z]$")
MAX_SLOT = 100
SLOT_STRIDE = 200
_LOCK = threading.Lock()


class InstanceError(ValueError):
    pass


def validate_name(name: Any) -> str:
    """A lower-case name: letters, digits and dashes, 1-32 characters, starting with a letter."""
    text = str(name or "").strip().lower()
    if not text or text == DEFAULT_INSTANCE:
        return DEFAULT_INSTANCE
    if not _NAME_RE.match(text):
        raise InstanceError(
            f"instance name {name!r}: use 1-32 lower-case letters, digits and dashes, starting with a letter "
            "(it becomes part of a folder, a service and a unit name)"
        )
    return text


def current_instance(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    try:
        return validate_name(env.get(INSTANCE_ENV))
    except InstanceError:
        # An invalid value in the environment never silently means "default": it fails at the CLI
        # (`apply_instance_arg`); a library reader treats it as the default.
        return DEFAULT_INSTANCE


def is_default(name: Optional[str] = None) -> bool:
    return (name or current_instance()) == DEFAULT_INSTANCE


def suffix(name: Optional[str] = None) -> str:
    """`""` for the default instance, `-NAME` otherwise."""
    n = name or current_instance()
    return "" if n == DEFAULT_INSTANCE else f"-{n}"


# ---------------------------------------------------------------------------
# The registry (slots)
# ---------------------------------------------------------------------------


def registry_path(env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None) -> Path:
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    if os.name == "nt":
        base = Path(str(env.get("APPDATA") or "").strip() or (home / "AppData" / "Roaming"))
    else:
        xdg = str(env.get("XDG_CONFIG_HOME") or "").strip()
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    return base / "abstractgateway" / "instances.json"


def _read(path: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema": REGISTRY_SCHEMA, "instances": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("instances"), dict):
        return {"schema": REGISTRY_SCHEMA, "instances": {}}
    return doc


def registered(env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    return dict(_read(registry_path(env, home))["instances"])


def slot(name: Optional[str] = None, *, env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None,
         assign: bool = True, record: Optional[Dict[str, Any]] = None) -> int:
    """The instance's slot: 0 for the default instance; else its registered slot, assigned (the
    smallest free one) and written on first use when `assign`. Raises InstanceError when no slot is
    free or (with `assign=False`) the instance is not registered."""
    n = validate_name(name or current_instance(env))
    if n == DEFAULT_INSTANCE:
        return 0
    path = registry_path(env, home)
    with _LOCK:
        doc = _read(path)
        rows = doc["instances"]
        row = rows.get(n) if isinstance(rows.get(n), dict) else None
        if row is not None and isinstance(row.get("slot"), int):
            if record:
                changed = {k: v for k, v in record.items() if row.get(k) != v}
                if changed:
                    row.update(changed)
                    _write(path, doc)
            return int(row["slot"])
        if not assign:
            raise InstanceError(f"instance {n!r} is not registered on this machine ({path})")
        used = {int(r.get("slot")) for r in rows.values() if isinstance(r, dict) and isinstance(r.get("slot"), int)}
        free = next((s for s in range(1, MAX_SLOT + 1) if s not in used), None)
        if free is None:
            raise InstanceError(f"no free instance slot (at most {MAX_SLOT} named instances)")
        rows[n] = dict(record or {}, slot=free, created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        _write(path, doc)
        return free


def forget(name: str, *, env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None) -> bool:
    """Drop an instance from the registry (its slot becomes free). True when it was there."""
    n = validate_name(name)
    if n == DEFAULT_INSTANCE:
        return False
    path = registry_path(env, home)
    with _LOCK:
        doc = _read(path)
        if n not in doc["instances"]:
            return False
        doc["instances"].pop(n, None)
        _write(path, doc)
        return True


def _write(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc["schema"] = REGISTRY_SCHEMA
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# What a slot means
# ---------------------------------------------------------------------------


def app_port_offset(name: Optional[str] = None, *, env: Optional[Mapping[str, str]] = None) -> int:
    """How far this instance's app ports sit from the default instance's (0, 200, 400, ...)."""
    return SLOT_STRIDE * slot(name, env=env)


def app_port_range(name: Optional[str] = None, *, env: Optional[Mapping[str, str]] = None) -> Tuple[int, int]:
    off = app_port_offset(name, env=env)
    return (3100 + off, 3199 + off)


DEFAULT_GATEWAY_PORT = 8080


def gateway_port(name: Optional[str] = None, *, env: Optional[Mapping[str, str]] = None) -> int:
    """The instance's default gateway port: 8080 for the default instance, 8090 + slot for a named one
    (an explicit `--port` / the stored Network setting still wins)."""
    s = slot(name, env=env)
    return DEFAULT_GATEWAY_PORT if s == 0 else 8090 + s


def service_names(name: Optional[str] = None) -> Dict[str, str]:
    s = suffix(name)
    dot = f".{(name or current_instance())}" if s else ""
    return {
        "systemd_unit": f"abstractgateway{s}.service",
        "launchd_label": f"ai.abstractframework.gateway{dot}",
        "xdg_autostart_file": f"abstractgateway{s}.desktop",
        "windows_run_value": f"AbstractGateway{s}",
    }


def apply_instance_arg(argv: list, env: Optional[MutableMapping[str, str]] = None) -> list:
    """`abstractgateway --instance NAME ...` (or `--instance=NAME`, anywhere before the command's own
    arguments): export ABSTRACTGATEWAY_INSTANCE and return argv without it. An invalid name exits 2."""
    env = os.environ if env is None else env
    out = []
    i = 0
    while i < len(argv):
        a = str(argv[i])
        value = None
        if a == "--instance" and i + 1 < len(argv):
            value = str(argv[i + 1])
            i += 2
        elif a.startswith("--instance="):
            value = a.split("=", 1)[1]
            i += 1
        else:
            out.append(argv[i])
            i += 1
            continue
        try:
            name = validate_name(value)
        except InstanceError as exc:
            raise SystemExit(f"abstractgateway: {exc}") from None
        if name == DEFAULT_INSTANCE:
            env.pop(INSTANCE_ENV, None)
        else:
            env[INSTANCE_ENV] = name
    return out


def apply_instance_env(data_dir: Path, env: Optional[MutableMapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """For a named instance: its own AbstractCore config dir (unless set) and its registry record.
    Returns the instance facts, or None for the default instance."""
    env = os.environ if env is None else env
    name = current_instance(env)
    if name == DEFAULT_INSTANCE:
        return None
    if not str(env.get("ABSTRACTCORE_CONFIG_DIR") or "").strip() and not str(env.get("ABSTRACTCORE_CONFIG_FILE") or "").strip():
        env["ABSTRACTCORE_CONFIG_DIR"] = str(Path(data_dir) / "core-config")
    s = slot(name, env=env, record={"data_dir": str(data_dir)})
    lo, hi = app_port_range(name, env=env)
    return {"name": name, "slot": s, "data_dir": str(data_dir), "core_config_dir": env.get("ABSTRACTCORE_CONFIG_DIR"),
            "app_ports": [3001 + SLOT_STRIDE * s, 3006 + SLOT_STRIDE * s], "app_port_range": [lo, hi],
            **service_names(name)}


def describe(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The facts `GET /about` and `config status` carry (no registry write)."""
    env = os.environ if env is None else env
    name = current_instance(env)
    out: Dict[str, Any] = {"name": name, "default": name == DEFAULT_INSTANCE}
    if name != DEFAULT_INSTANCE:
        try:
            out["slot"] = slot(name, env=env, assign=False)
        except InstanceError:
            out["slot"] = None
        out.update(service_names(name))
    return out
