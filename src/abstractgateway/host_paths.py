"""Where a gateway keeps its data when nobody said.

Before this module, an unset ``ABSTRACTGATEWAY_DATA_DIR`` meant ``./runtime``
relative to the WORKING DIRECTORY. That is right for a developer checkout (the
root ``scripts/`` and every contributor's muscle memory) and wrong for anything
a service manager starts: launchd and systemd start processes in ``/`` and
Windows in ``C:\\Windows\\System32``, so the data landed somewhere nobody would
look, or nowhere writable at all.

The resolution, in order:

1. ``ABSTRACTGATEWAY_DATA_DIR`` (then the legacy ``ABSTRACTFLOW_RUNTIME_DIR`` /
   ``ABSTRACTFLOW_GATEWAY_DATA_DIR``): the operator said, the operator wins.
2. ``./runtime`` when it already EXISTS in the working directory: the
   developer-checkout layout keeps working exactly as before.
3. The per-OS user data directory:
   - macOS ``~/Library/Application Support/AbstractGateway``
   - Linux ``$XDG_DATA_HOME/abstractgateway`` (``~/.local/share/abstractgateway``)
   - Windows ``%LOCALAPPDATA%\\AbstractGateway``

Every answer carries its ``source`` and a one-line ``reason`` so the CLI, the
console (``/host/state``) and ``abstractgateway-config status --json`` can say
which directory was chosen and why, instead of leaving the operator to guess.

The flows directory needs no such treatment: its default is resolved relative
to the installed package (``config._default_flows_dir``), never to the CWD.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional

# Canonical first, then the legacy names `config.GatewayHostConfig` and
# `users.gateway_data_dir_from_env` have always accepted.
DATA_DIR_ENV_NAMES: tuple[str, ...] = (
    "ABSTRACTGATEWAY_DATA_DIR",
    "ABSTRACTFLOW_RUNTIME_DIR",
    "ABSTRACTFLOW_GATEWAY_DATA_DIR",
)

# Internal marker: when a CLI entry point EXPORTS the resolved default into
# ABSTRACTGATEWAY_DATA_DIR (so every in-process reader and every child process
# agrees), it records how that value was chosen here. Without it, the exported
# value would read back as "the operator set it", which is not true.
DATA_DIR_SOURCE_ENV = "ABSTRACTGATEWAY_DATA_DIR_SOURCE"

SOURCE_ENV = "env"
SOURCE_LEGACY_CWD = "legacy_cwd_runtime"
SOURCE_OS_DEFAULT = "os_default"

LEGACY_CWD_DIRNAME = "runtime"


@dataclass(frozen=True)
class DataDirResolution:
    path: Path
    source: str  # env | legacy_cwd_runtime | os_default
    reason: str
    env_name: Optional[str] = None

    def public_dict(self) -> Dict[str, Any]:
        return {
            "data_dir": str(self.path),
            "data_dir_source": self.source,
            "data_dir_reason": self.reason,
            "data_dir_env": self.env_name,
        }


def normalize_platform(system: Optional[str] = None) -> str:
    """`darwin` | `windows` | `linux` (every other POSIX is treated as linux)."""
    raw = str(system if system is not None else sys.platform).strip().lower()
    if raw.startswith("darwin") or raw == "macos":
        return "darwin"
    if raw.startswith("win") or raw.startswith("cygwin") or raw.startswith("msys"):
        return "windows"
    return "linux"


def user_data_dir(
    *,
    system: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """The per-OS, per-user data directory for AbstractGateway (not created)."""
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    plat = normalize_platform(system)
    if plat == "darwin":
        return home / "Library" / "Application Support" / "AbstractGateway"
    if plat == "windows":
        local = str(env.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "AbstractGateway"
    xdg = str(env.get("XDG_DATA_HOME") or "").strip()
    # The XDG spec: a relative XDG_DATA_HOME is invalid and must be ignored.
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "share"
    return base / "abstractgateway"


def user_cache_dir(
    *,
    system: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """The per-OS, per-user CACHE directory for AbstractGateway (not created).

    Re-creatable downloads (engine installers, wheels, staging) belong here,
    not in the data directory:

    - macOS ``~/Library/Caches/AbstractGateway``
    - Linux ``$XDG_CACHE_HOME/abstractgateway`` (``~/.cache/abstractgateway``)
    - Windows ``%LOCALAPPDATA%\\AbstractGateway\\Cache``
    """
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    plat = normalize_platform(system)
    if plat == "darwin":
        return home / "Library" / "Caches" / "AbstractGateway"
    if plat == "windows":
        local = str(env.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "AbstractGateway" / "Cache"
    xdg = str(env.get("XDG_CACHE_HOME") or "").strip()
    # The XDG spec: a relative XDG_CACHE_HOME is invalid and must be ignored.
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".cache"
    return base / "abstractgateway"


def _platform_label(system: Optional[str]) -> str:
    return {"darwin": "macOS", "windows": "Windows", "linux": "Linux"}[normalize_platform(system)]


def _read_marker(env: Mapping[str, str], value: str) -> Optional[Dict[str, str]]:
    raw = str(env.get(DATA_DIR_SOURCE_ENV) or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # The marker only describes the value it was written for: an operator who
    # later exports a different ABSTRACTGATEWAY_DATA_DIR in a child shell
    # inherits a stale marker, and that value IS theirs.
    try:
        same = Path(str(data.get("path") or "")).expanduser().resolve() == Path(value).expanduser().resolve()
    except Exception:
        same = False
    if not same:
        return None
    source = str(data.get("source") or "").strip()
    if source not in {SOURCE_LEGACY_CWD, SOURCE_OS_DEFAULT}:
        return None
    return {"source": source, "reason": str(data.get("reason") or "")}


def resolve_data_dir(
    *,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
    system: Optional[str] = None,
    home: Optional[Path] = None,
) -> DataDirResolution:
    """Which data directory a gateway started here would use, and why."""
    env = os.environ if env is None else env
    for name in DATA_DIR_ENV_NAMES:
        value = str(env.get(name) or "").strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if name == DATA_DIR_ENV_NAMES[0]:
            marker = _read_marker(env, value)
            if marker is not None:
                return DataDirResolution(path=path, source=marker["source"], reason=marker["reason"], env_name=None)
        return DataDirResolution(path=path, source=SOURCE_ENV, reason=f"{name} is set", env_name=name)

    base = Path(cwd) if cwd is not None else Path.cwd()
    legacy = base / LEGACY_CWD_DIRNAME
    try:
        legacy_exists = legacy.is_dir()
    except OSError:
        legacy_exists = False
    if legacy_exists:
        return DataDirResolution(
            path=legacy.resolve(),
            source=SOURCE_LEGACY_CWD,
            reason=(
                f"./{LEGACY_CWD_DIRNAME} exists in the working directory {base} (developer checkout); "
                "kept for backward compatibility. Set ABSTRACTGATEWAY_DATA_DIR to choose explicitly."
            ),
        )
    path = user_data_dir(system=system, env=env, home=home)
    return DataDirResolution(
        path=path,
        source=SOURCE_OS_DEFAULT,
        reason=f"ABSTRACTGATEWAY_DATA_DIR is unset and no ./{LEGACY_CWD_DIRNAME} in the working directory: "
        f"{_platform_label(system)} per-user data directory",
    )


def default_data_dir() -> Path:
    """The data dir when nothing is exported (the fallback every reader shares)."""
    return resolve_data_dir().path


def apply_data_dir_default(env: Optional[MutableMapping[str, str]] = None) -> DataDirResolution:
    """Resolve once and EXPORT the answer, so every reader in this process and
    every child it spawns (runner, tray, entity seats) agrees on one directory.

    A value the operator set is left untouched. An exported default records its
    provenance in `ABSTRACTGATEWAY_DATA_DIR_SOURCE` so later readers still
    report "os_default" / "legacy_cwd_runtime" rather than "env"."""
    env = os.environ if env is None else env
    res = resolve_data_dir(env=env)
    if res.source == SOURCE_ENV:
        return res
    env["ABSTRACTGATEWAY_DATA_DIR"] = str(res.path)
    env[DATA_DIR_SOURCE_ENV] = json.dumps(
        {"source": res.source, "path": str(res.path), "reason": res.reason}, ensure_ascii=False
    )
    return res


def describe_resolution(res: DataDirResolution) -> str:
    """One stderr line for boot logs."""
    return f"Gateway data dir: {res.path} ({res.source}: {res.reason})"
